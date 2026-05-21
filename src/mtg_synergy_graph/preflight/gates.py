"""Pre-flight gates. v1.0 ships ``stage_a_golden_coverage`` only.

Stage A — golden-coverage prefilter
-----------------------------------

For a candidate signature ``(port_type, event_class, sub_discriminator)``,
Stage A counts distinct legendary-creature commanders whose ports satisfy
the gate, in two corpora:

1. The 500-cmdr fixture at ``tests/fixtures/golden_set_run_500.json``
   (the project's evaluation surface).
2. The full legal-commander universe (``cards.legal_commander = 1`` AND
   the card is a legendary creature).

Verdict matrix:

==================  =====================  =====================  =========
fixture commanders  legal-universe count   Verdict                Severity
==================  =====================  =====================  =========
``>= 1``            (any)                  ``PASS``               PASS
``0``               ``>= 3``               ``FIXTURE_BLIND_SPOT`` WARN
``0``               ``< 3``                ``UNTESTABLE``         REJECT
==================  =====================  =====================  =========

The legal-universe second corpus distinguishes "rule is structurally
legitimate but our 500-fixture happens not to cover it" (downgrade to
WARN, FIXTURE_BLIND_SPOT) from "rule fires on essentially nothing in
the entire universe" (REJECT, UNTESTABLE). The canonical save case
``damage_prevention_voltron`` (2026-04-25) hits 0 fixture commanders but
~31 legal-universe commanders, so it produces FIXTURE_BLIND_SPOT — flagging
to the human/walker that the rule is real but not measurable on the
current fixture, without hard-blocking.

Defensive note: synergy.db files created before the ``legal_commander``
column was added (per the engine.py:172-195 precedent) trigger a fallback
to fixture-only counting. Under that fallback, FIXTURE_BLIND_SPOT cannot
fire — REJECT is emitted whenever fixture count is zero. The fallback is
logged so the operator knows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .types import Candidate, GateVerdict, Severity

_LOGGER = logging.getLogger(__name__)

#: Default path to the 500-cmdr fixture. Tests can override via the
#: ``fixture_path`` parameter on ``stage_a_golden_coverage``.
DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "tests" / "fixtures" / "golden_set_run_500.json"
)

#: Threshold for "≥3" legal-universe commanders triggering
#: FIXTURE_BLIND_SPOT instead of UNTESTABLE. Below this count we treat
#: the rule as effectively untestable in any meaningful sense.
LEGAL_UNIVERSE_BLIND_SPOT_THRESHOLD = 3


def _load_fixture_commanders(fixture_path: Path) -> list[str]:
    """Read the 500-cmdr fixture and return commander names.

    The fixture has shape ``{"entries": [{"commander": "...", ...}, ...]}``.
    """
    with fixture_path.open() as fh:
        data = json.load(fh)
    return [entry["commander"] for entry in data.get("entries", [])]


def _has_legal_commander_column(conn: sqlite3.Connection) -> bool:
    """Detect whether ``cards`` has the ``legal_commander`` column.

    Mirrors the defensive PRAGMA pattern at engine.py:172-195 — synergy.db
    files created before the legal_commander migration don't have the column.
    """
    rows = conn.execute("PRAGMA table_info(cards)").fetchall()
    return any(r[1] == "legal_commander" for r in rows)


def _signature_to_sql_predicate(
    signature: tuple[str, str, str],
) -> tuple[str, list[str | int]]:
    """Translate a port signature to an SQL WHERE predicate fragment.

    Mirrors the inverse of ``scripts.gap_report._port_signature``:

    - replacement signatures match ``port_type='replacement' AND
      event_class=? AND replacement_result=?``.
    - ChangesZone signatures (sub_discriminator like ``"origin->dest"``)
      match ``port_type=? AND event_class=? AND zone_origin=? AND
      zone_destination=?``.
    - plain signatures with a qualifier match ``port_type=? AND event_class=?
      AND valid_filter LIKE ?`` (qualifier as substring).
    - plain signatures with empty sub match ``port_type=? AND event_class=?``.

    Returns ``(predicate_sql, params)`` where ``predicate_sql`` is a
    parenthesized expression suitable for ``WHERE ... AND (predicate)``.
    """
    port_type, event_class, sub = signature

    if port_type == "replacement":
        return (
            "(port_type = ? AND event_class = ? AND replacement_result = ?)",
            [port_type, event_class, sub],
        )

    if event_class in ("ChangesZone", "ChangeZone", "ChangesZoneAll", "ChangeZoneAll") and sub and "->" in sub:
        zo, zd = sub.split("->", 1)
        return (
            "(port_type = ? AND event_class = ? AND zone_origin = ? AND zone_destination = ?)",
            [port_type, event_class, zo, zd],
        )

    if sub:
        # Match the notable qualifier as a substring of valid_filter.
        # gap_report's _notable_qualifier extracts the qualifier from
        # the same valid_filter; LIKE %sub% is the inverse.
        return (
            "(port_type = ? AND event_class = ? AND valid_filter LIKE ?)",
            [port_type, event_class, f"%{sub}%"],
        )

    return ("(port_type = ? AND event_class = ?)", [port_type, event_class])


def _count_distinct_commanders(
    conn: sqlite3.Connection,
    predicate_sql: str,
    predicate_params: list[str | int],
    *,
    restrict_to: list[str] | None = None,
    require_legal: bool = False,
) -> int:
    """Count distinct legendary-creature commanders whose ports satisfy
    the predicate.

    ``restrict_to`` (when provided) filters to a fixture commander list.
    ``require_legal`` adds the ``legal_commander = 1`` filter for the
    legal-universe count.
    """
    params: list[str | int] = list(predicate_params)
    extra_clauses = []

    if restrict_to:
        placeholders = ",".join("?" * len(restrict_to))
        extra_clauses.append(f"cards.name IN ({placeholders})")
        params.extend(restrict_to)
    else:
        extra_clauses.append("cards.types LIKE '%Legendary%'")
        extra_clauses.append("cards.types LIKE '%Creature%'")

    if require_legal:
        extra_clauses.append("cards.legal_commander = 1")

    where_extras = " AND " + " AND ".join(extra_clauses) if extra_clauses else ""

    # predicate_sql + where_extras are built internally from a fixed
    # vocabulary (port_type/event_class/replacement_result/zone_origin/
    # zone_destination + LIKE/IN clauses with parameterized values). No
    # user-supplied SQL fragments enter this string. S608 is a false
    # positive here.
    base = "SELECT COUNT(DISTINCT cards.name) FROM cards JOIN card_ports ON card_ports.card_name = cards.name WHERE "
    sql = base + predicate_sql + where_extras
    return conn.execute(sql, params).fetchone()[0]


def stage_a_golden_coverage(
    candidate: Candidate,
    conn: sqlite3.Connection,
    *,
    fixture_path: Path | None = None,
    legal_blind_spot_threshold: int = LEGAL_UNIVERSE_BLIND_SPOT_THRESHOLD,
) -> GateVerdict:
    """Run Stage A's two-corpus golden-coverage check.

    Returns a ``GateVerdict`` with ``name='stage_a'`` per the verdict
    matrix in the module docstring.

    Raises:
        ValueError: if ``candidate.signature[0]`` (port_type) is empty
            (the predicate would be vacuous).
    """
    if not candidate.signature[0]:
        raise ValueError(f"Candidate signature has empty port_type: {candidate.signature!r}")

    predicate_sql, predicate_params = _signature_to_sql_predicate(candidate.signature)

    fixture_path = fixture_path or DEFAULT_FIXTURE_PATH
    fixture_commanders = _load_fixture_commanders(fixture_path)
    fixture_count = _count_distinct_commanders(conn, predicate_sql, predicate_params, restrict_to=fixture_commanders)

    if fixture_count >= 1:
        return GateVerdict(
            name="stage_a",
            severity=Severity.PASS,
            reason=f"PASS: {fixture_count} fixture commanders match the gate",
        )

    # Fixture count is 0; consult the legal universe second corpus.
    if not _has_legal_commander_column(conn):
        _LOGGER.warning(
            "stage_a: cards.legal_commander column absent; falling back to "
            "fixture-only counting. FIXTURE_BLIND_SPOT verdict cannot fire "
            "under this fallback."
        )
        return GateVerdict(
            name="stage_a",
            severity=Severity.REJECT,
            reason="UNTESTABLE: 0 fixture commanders (legal_commander column absent — fallback)",
        )

    legal_count = _count_distinct_commanders(conn, predicate_sql, predicate_params, require_legal=True)

    if legal_count >= legal_blind_spot_threshold:
        return GateVerdict(
            name="stage_a",
            severity=Severity.WARN,
            reason=(
                f"FIXTURE_BLIND_SPOT: 0 fixture commanders but "
                f"{legal_count} legal-universe commanders match — rule is "
                f"structurally legitimate but unmeasurable on the current fixture"
            ),
        )

    return GateVerdict(
        name="stage_a",
        severity=Severity.REJECT,
        reason=(
            f"UNTESTABLE: 0 fixture commanders and only {legal_count} "
            f"legal-universe commanders (< {legal_blind_spot_threshold} threshold)"
        ),
    )
