"""Deterministic effect-per-mana rate signal (plan 2026-07-06-001 Phase C).

Built entirely from Forge-extracted data already in synergy.db:
``card_ports.amount`` magnitudes (effect/static rows), an engine-shape
marker (any trigger or activation-cost port -> repeatable), and cmc.
No EDHREC, no popularity, no hand-curated card list. Design-time
kill-test only until the Phase C gate passes.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict

#: Cards with any of these ports have a repeatable engine shape; pure
#: one-shot spells get half weight — a Divination is worth less per
#: mana than a "draw each turn" engine at the same printed amount.
_ENGINE_MARKER_PORTS = frozenset({"trigger", "cost"})
_AMOUNT_BEARING_PORTS = frozenset({"effect", "static"})
_ONE_SHOT_WEIGHT = 0.5
_VARIABLE_AMOUNT_VALUE = 2.5  # X/Y/Z — scales with investment
_ALL_AMOUNT_VALUE = 4.0  # "All" — board-scope effects
_AMOUNT_CAP = 6.0
_DEFAULT_CMC = 4.0


def _amount_value(amount: str) -> float:
    if amount in ("X", "Y", "Z"):
        return _VARIABLE_AMOUNT_VALUE
    if amount == "All":
        return _ALL_AMOUNT_VALUE
    try:
        v = float(amount)
    except ValueError:
        return 1.0
    return min(max(v, 0.0), _AMOUNT_CAP)


def quality_multiplier(rate: float, *, q: float, r0: float) -> float:
    """Bounded multiplicative prior: 1.0 at rate 0, asymptote 1+q."""
    return 1.0 + q * math.tanh(rate / r0)


def rate_signal(conn: sqlite3.Connection) -> dict[str, float]:
    output: dict[str, float] = defaultdict(float)
    engine_shape: set[str] = set()
    for name, ptype, amount in conn.execute("SELECT card_name, port_type, amount FROM card_ports"):
        if ptype in _ENGINE_MARKER_PORTS:
            engine_shape.add(name)
        if ptype in _AMOUNT_BEARING_PORTS and amount:
            output[name] += _amount_value(amount)
    cmc = {n: (c if c is not None else _DEFAULT_CMC) for n, c in conn.execute("SELECT name, cmc FROM cards")}
    return {
        name: (1.0 if name in engine_shape else _ONE_SHOT_WEIGHT) * out / max(cmc.get(name, _DEFAULT_CMC), 1.0)
        for name, out in output.items()
    }
