"""Per-subkind forge signal for ``gap_report.py`` re-ranking.

Reads ``forge_precon_ppmi`` from the sidecar DB, finds each subkind's
strongest partner (max PPMI across all pairs the subkind participates
in), and normalizes the result into ``[0.5, 1.5]`` so the gap-report
sort key can multiply ``impact × forge_signal`` without either sign
swamping the other.

Normalization strategy:

- The maximum PPMI per subkind is the signal (not the mean — a subkind
  that has one strong partner AND many weak ones should rank high, not
  be diluted).
- The 95th-percentile max-PPMI across the corpus is mapped to 1.5.
- Anything at or above p95 → 1.5 (capped boost).
- Zero max-PPMI → 1.0 (no penalty, just absence of boost).
- In between: linear interpolation between 1.0 and 1.5.

The plan allows 0.5 as a dampening floor, but the MVP never emits
values below 1.0 because forge absence is not evidence of low synergy
— a subkind may simply be under-represented in the precon corpus.
Dampening below 1.0 would be surprising behavior for a rule-authoring
tool. The ceiling at 1.5 prevents any single subkind from dominating
the rank; audits can tune the ceiling if empirical review suggests
they should.

This module is read-only; it never writes to the sidecar. Failure
modes — missing file, corrupt DB, empty table, missing table — all
degrade to an empty signal dict (silent fallback), so
``gap_report.py`` can always produce a report.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 6.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

_LOG = logging.getLogger(__name__)

_WEIGHT_FLOOR = 1.0  # MVP: no dampening; see module docstring.
_WEIGHT_CEILING = 1.5


def load_forge_signals(db_path: Path) -> dict[str, float]:
    """Return ``{subkind: weight}`` normalized into ``[1.0, 1.5]``.

    Silent fallback on missing/corrupt/empty sidecar (returns ``{}``)
    — ``gap_report.py`` logs a warning but never fails when the
    sidecar is unavailable.
    """
    if not db_path.is_file():
        return {}

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.DatabaseError as exc:
        _LOG.warning("forge_oracle.db failed to open: %s", exc)
        return {}

    try:
        # Check table exists (may not on a DB built by a very early pre-Unit-4 run).
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='forge_precon_ppmi'"
        ).fetchone()
        if exists is None:
            return {}
        rows = conn.execute(
            "SELECT subkind, MAX(ppmi) AS max_ppmi FROM ("
            "  SELECT port_signature_a AS subkind, ppmi FROM forge_precon_ppmi "
            "  UNION ALL "
            "  SELECT port_signature_b AS subkind, ppmi FROM forge_precon_ppmi"
            ") GROUP BY subkind"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        _LOG.warning("forge_oracle.db read failed: %s", exc)
        return {}
    finally:
        conn.close()

    if not rows:
        return {}

    positives = sorted(v for (_s, v) in rows if v is not None and v > 0)
    if not positives:
        return {}

    # 95th percentile across the positive distribution; cap at highest value
    # when the corpus is too small for a stable percentile.
    p95 = positives[int(0.95 * len(positives))] if len(positives) >= 20 else positives[-1]
    if p95 <= 0:
        return {}

    out: dict[str, float] = {}
    for subkind, raw in rows:
        if raw is None or raw <= 0:
            continue
        normalized = min(raw / p95, 1.0)  # [0, 1]
        weight = _WEIGHT_FLOOR + normalized * (_WEIGHT_CEILING - _WEIGHT_FLOOR)
        out[subkind] = weight
    return out


def forge_weight_for_signature(
    signature: tuple[str, str, str],
    signals: dict[str, float],
) -> float:
    """Map a gap-report ``(port_type, event_class, sub_discriminator)``
    signature to its forge signal weight.

    Returns ``1.0`` (neutral) when the subkind ``port_type.event_class``
    is absent from ``signals``. The sub-discriminator is ignored — the
    forge PPMI subkind collapses over it by construction (``port_nodes``
    view defines ``subkind = port_type || '.' || event_class``).
    """
    pt, ev, _sub = signature
    subkind = f"{pt}.{ev}"
    return signals.get(subkind, 1.0)
