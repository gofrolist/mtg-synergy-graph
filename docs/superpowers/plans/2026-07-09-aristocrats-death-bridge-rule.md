# Aristocrats Death-Bridge Rule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated, flag-gated `aristocrats_death_bridge` complement rule that connects sacrifice-outlet / death-engine commanders to the aristocrats death-value class (death-triggered payoffs + self-recursive fodder) that currently sits unranked for them.

**Architecture:** A new focused module `complement_rules/aristocrats.py` holding a pure commander gate + a two-tier emitter; two skip-when-off `CandidateCache` pools in `penalties.py`; flag-off hash-neutral wiring through `core.py` + a flag-aware `RuleGate` in `registry.py`; a cohort predicate + pinned fixture + noise band for measurement. The rule ships **flag-off** (config hash unchanged) as standing infrastructure; it flips on ONLY if the pre-registered gates pass.

**Tech Stack:** Python 3.13, sqlite3, pytest, `uv`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-09-aristocrats-death-bridge-rule-design.md`

## Global Constraints

- **Hash-neutral through Tasks 1–5.** `_ENABLE_ARISTOCRATS_DEATH_BRIDGE = False`; the flag is NOT added to `ScoringConfigInputs`/`compute_config_hash`; `config_hash` stays `c770b664e626`; `uv run scripts/bench.py audit --expect-identity` PASSES at every task boundary.
- `rule_id = "aristocrats_death_bridge"` is **NOT** in `DECLARATIVE_RULE_IDS` (Python-helper path only; the `_DUAL_PATH_OVERLAP` guard raises otherwise).
- **CandidateCache is a frozen dataclass:** new frozenset fields MUST be defaulted `= frozenset()` AND placed at the END of the class body.
- **Skip-when-off:** both pools load via `_bulk_load_...(conn) if _aristocrats_death_bridge_enabled() else frozenset()` so a flag-off build does zero extra scans.
- **Validated tier predicates (measured at `c770b664e626`):** tier1 `death_payoff` = 218 cards; tier2 `recursive_fodder` = 131 cards. All three tier-1 conditions (value-effect `execute_ref` join AND `Creature`-scope broader-than-self AND opponent-exclusion) are required — dropping the scope condition reintroduces an ~696-card flood.
- **`_DEATH_VALUE_EFFECTS`** = `frozenset({"LoseLife", "GainLife", "DealDamage", "DamageAll", "PutCounter", "PutCounterAll", "Token", "Draw", "Mana"})`.
- Tests never pass a literal project-relative DB path to `open_db()` — construct DBs in `tmp_path`, or use the `LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "synergy.db"` + `skipif` pattern for live-DB reads.
- SQL value lists are passed as **bound parameters** (`",".join("?" * len(...))`), never string-interpolated. Module-constant-only concatenation (e.g. `LEGAL_LEGENDARY_CREATURE_WHERE`) may carry `# noqa: S608`.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3`.

---

### Task 1: Commander gate + module scaffold

**Files:**
- Create: `src/mtg_synergy_graph/complement_rules/aristocrats.py`
- Test: `tests/complement_rules/test_aristocrats_death_bridge.py`

**Interfaces:**
- Produces: `_ENABLE_ARISTOCRATS_DEATH_BRIDGE: bool` (module flag, default `False`); `_commander_is_aristocrats(cmdr_ports: list[PortRow]) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/complement_rules/test_aristocrats_death_bridge.py
from mtg_synergy_graph.complement_rules import aristocrats as arm
from mtg_synergy_graph.complement_rules.aristocrats import _commander_is_aristocrats


def _port(port_type, event_class, **kw):
    base = {"port_type": port_type, "event_class": event_class,
            "cost_subtype": "", "cost_target": "", "zone_origin": "",
            "zone_destination": "", "valid_filter": ""}
    base.update(kw)
    return base


def test_sac_outlet_creature_other_qualifies():
    ports = [_port("cost", "sacrifice", cost_subtype="1/Creature.Other/another creature", cost_target="other")]
    assert _commander_is_aristocrats(ports) is True


def test_sac_outlet_creature_any_qualifies():
    ports = [_port("cost", "sacrifice", cost_subtype="1/Creature", cost_target="any")]
    assert _commander_is_aristocrats(ports) is True


def test_death_trigger_payoff_qualifies():
    ports = [_port("trigger", "ChangesZone", zone_origin="Battlefield", zone_destination="Graveyard")]
    assert _commander_is_aristocrats(ports) is True


def test_sac_self_only_rejected():
    # Sacrificing itself (e.g. a fling body) is not a sac-outlet engine.
    ports = [_port("cost", "sacrifice", cost_subtype="1/CARDNAME", cost_target="self")]
    assert _commander_is_aristocrats(ports) is False


def test_sac_artifact_rejected():
    ports = [_port("cost", "sacrifice", cost_subtype="1/Artifact", cost_target="any")]
    assert _commander_is_aristocrats(ports) is False


def test_etb_changeszone_not_death_rejected():
    # Battlefield->Exile or ->Hand is not a death trigger.
    ports = [_port("trigger", "ChangesZone", zone_origin="Battlefield", zone_destination="Exile")]
    assert _commander_is_aristocrats(ports) is False


def test_empty_ports_rejected():
    assert _commander_is_aristocrats([]) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/complement_rules/test_aristocrats_death_bridge.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_synergy_graph.complement_rules.aristocrats'`

- [ ] **Step 3: Write the module scaffold + gate**

```python
# src/mtg_synergy_graph/complement_rules/aristocrats.py
"""Aristocrats death-bridge rule (spec 2026-07-09).

Bridges sacrifice-outlet / death-engine commanders to the aristocrats
death-value class — death-triggered payoffs (Blood Artist, Zulaport) and
self-recursive fodder (Reassembling Skeleton, Butcher Ghoul) — that the current
vocabulary leaves unranked because the substrate has no
``sacrifice -> ChangesZone(bf->grave)`` equivalence. Dedicated flag-gated,
own-pool implementation: does NOT touch the shared event-match substrate.
Default OFF; config-hash-neutral until a measured SHIP flip.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .core import PortComplement, PortRow

if TYPE_CHECKING:
    from ..penalties import CandidateCache

#: Default-OFF flag. NOT registered in ScoringConfigInputs — flipping it does
#: not change config_hash until the SHIP commit adds the scoring_weights entry.
_ENABLE_ARISTOCRATS_DEATH_BRIDGE: bool = False


def _commander_is_aristocrats(cmdr_ports: list[PortRow]) -> bool:
    """Unit 1 gate: the commander establishes a death/sacrifice engine.

    True iff some port is EITHER
    * a creature sacrifice outlet — ``cost``/``sacrifice`` whose ``cost_subtype``
      references a Creature and whose ``cost_target`` is not ``self``; OR
    * a death-trigger payoff — ``trigger``/``ChangesZone`` Battlefield->Graveyard.
    """
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "cost" and ev == "sacrifice":
            sub = p.get("cost_subtype") or ""
            tgt = (p.get("cost_target") or "").strip()
            if "Creature" in sub and tgt != "self":
                return True
        if pt == "trigger" and ev == "ChangesZone":
            zo = p.get("zone_origin") or ""
            zd = p.get("zone_destination") or ""
            if "Battlefield" in zo and "Graveyard" in zd:
                return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/complement_rules/test_aristocrats_death_bridge.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mtg_synergy_graph/complement_rules/aristocrats.py tests/complement_rules/test_aristocrats_death_bridge.py
git commit -m "feat(aristocrats): commander gate _commander_is_aristocrats + module scaffold"
```

---

### Task 2: Candidate emitter + skip-when-off pools

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/aristocrats.py`
- Modify: `src/mtg_synergy_graph/penalties.py`
- Test: `tests/complement_rules/test_aristocrats_death_bridge.py` (extend)

**Interfaces:**
- Consumes: `_commander_is_aristocrats` (Task 1); `CandidateCache` (penalties).
- Produces: `_find_aristocrats_death_bridge(conn, cmdr_ports, cmdr_set, candidate_cache=None) -> list[PortComplement]`; `penalties._DEATH_VALUE_EFFECTS`; `penalties._aristocrats_death_bridge_enabled()`; `penalties._bulk_load_aristocrats_death_payoff_cards(conn)`; `penalties._bulk_load_aristocrats_recursive_fodder_cards(conn)`; two new `CandidateCache` fields `aristocrats_death_payoff_cards`, `aristocrats_recursive_fodder_cards`.

- [ ] **Step 1: Write the failing emitter tests**

Append to `tests/complement_rules/test_aristocrats_death_bridge.py`:

```python
import sqlite3
import pytest


@pytest.fixture()
def arm_conn(monkeypatch):
    # Emitter tests exercise the firing path — enable the default-OFF flag.
    monkeypatch.setattr(arm, "_ENABLE_ARISTOCRATS_DEATH_BRIDGE", True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE cards (name TEXT, card_types TEXT);
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY, card_name TEXT, port_type TEXT,
            event_class TEXT, granted_keyword TEXT, valid_filter TEXT,
            zone_origin TEXT, zone_destination TEXT, execute_ref TEXT,
            source_svar TEXT
        );
        """
    )
    conn.executescript(
        """
        INSERT INTO cards VALUES ('Blood Artist', 'Creature');
        INSERT INTO cards VALUES ('Self Death Draw', 'Creature');
        INSERT INTO cards VALUES ('Opponent Payoff', 'Creature');
        INSERT INTO cards VALUES ('Reassembling Skeleton', 'Creature');
        INSERT INTO cards VALUES ('Undying Body', 'Creature');
        INSERT INTO cards VALUES ('Sun Titan', 'Creature');
        INSERT INTO cards VALUES ('Plain Bear', 'Creature');
        """
    )
    conn.executescript(
        """
        -- Blood Artist: death payoff watching OTHER creatures, drains via execute_ref
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref, source_svar)
          VALUES ('Blood Artist', 'trigger', 'ChangesZone', 'Card.Self,Creature.Other', 'Battlefield', 'Graveyard', 'TrigLoseLife', NULL);
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('Blood Artist', 'effect', 'LoseLife', 'TrigLoseLife');
        -- Self Death Draw: triggers only on ITSELF dying -> one-shot, EXCLUDED
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref)
          VALUES ('Self Death Draw', 'trigger', 'ChangesZone', 'Card.Self', 'Battlefield', 'Graveyard', 'TrigDraw');
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('Self Death Draw', 'effect', 'Draw', 'TrigDraw');
        -- Opponent Payoff: watches only opponents' creatures dying -> EXCLUDED
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref)
          VALUES ('Opponent Payoff', 'trigger', 'ChangesZone', 'Creature.OppCtrl', 'Battlefield', 'Graveyard', 'TrigDmg');
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('Opponent Payoff', 'effect', 'DealDamage', 'TrigDmg');
        -- Reassembling Skeleton: self-return grave->bf (no valid_filter) -> tier2
        INSERT INTO card_ports (card_name, port_type, event_class, zone_origin, zone_destination)
          VALUES ('Reassembling Skeleton', 'effect', 'ChangeZone', 'Graveyard', 'Battlefield');
        -- Undying Body: keyword fodder -> tier2
        INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword)
          VALUES ('Undying Body', 'keyword', 'Undying', 'Undying');
        -- Sun Titan: reanimator returning OTHER cards -> EXCLUDED from tier2
        INSERT INTO card_ports (card_name, port_type, event_class, zone_origin, zone_destination, valid_filter)
          VALUES ('Sun Titan', 'effect', 'ChangeZone', 'Graveyard', 'Battlefield', 'Permanent.YouCtrl+cmcLE3');
        """
    )
    conn.commit()
    return conn


_SAC = {"port_type": "cost", "event_class": "sacrifice", "cost_subtype": "1/Creature.Other/another creature",
        "cost_target": "other", "zone_origin": "", "zone_destination": "", "valid_filter": ""}


def test_emitter_death_payoff_tier_fires(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge
    comps = _find_aristocrats_death_bridge(arm_conn, [_SAC], set())
    by = {c.candidate: c for c in comps}
    assert by["Blood Artist"].rule_id == "aristocrats_death_bridge"
    assert by["Blood Artist"].cand_event == "death_payoff"
    assert by["Blood Artist"].cmdr_event == "death_engine"


def test_emitter_recursive_fodder_tier_fires(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge
    got = {c.candidate: c.cand_event for c in _find_aristocrats_death_bridge(arm_conn, [_SAC], set())}
    assert got.get("Reassembling Skeleton") == "recursive_fodder"
    assert got.get("Undying Body") == "recursive_fodder"


def test_emitter_exclusions(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge
    got = {c.candidate for c in _find_aristocrats_death_bridge(arm_conn, [_SAC], set())}
    assert "Self Death Draw" not in got   # self-death one-shot
    assert "Opponent Payoff" not in got    # opponent-scoped
    assert "Sun Titan" not in got          # reanimator returning other cards
    assert "Plain Bear" not in got         # no relevant port


def test_emitter_one_complement_per_candidate(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge
    comps = _find_aristocrats_death_bridge(arm_conn, [_SAC], set())
    assert len(comps) == len({c.candidate for c in comps})


def test_emitter_excludes_commander_itself(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge
    got = {c.candidate for c in _find_aristocrats_death_bridge(arm_conn, [_SAC], {"Blood Artist"})}
    assert "Blood Artist" not in got


def test_emitter_non_qualifying_commander_empty(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge
    non = {"port_type": "cost", "event_class": "sacrifice", "cost_subtype": "1/Artifact",
           "cost_target": "any", "zone_origin": "", "zone_destination": "", "valid_filter": ""}
    assert _find_aristocrats_death_bridge(arm_conn, [non], set()) == []


def test_emitter_flag_off_returns_empty(arm_conn, monkeypatch):
    monkeypatch.setattr(arm, "_ENABLE_ARISTOCRATS_DEATH_BRIDGE", False)
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge
    assert _find_aristocrats_death_bridge(arm_conn, [_SAC], set()) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/complement_rules/test_aristocrats_death_bridge.py -q`
Expected: FAIL — `ImportError: cannot import name '_find_aristocrats_death_bridge'`

- [ ] **Step 3: Add the loaders + `_DEATH_VALUE_EFFECTS` + enabled + cache fields in `penalties.py`**

Add near the other `_bulk_load_*` helpers (after `_bulk_load_x_cost_cost_reduce_cards` / `_x_cost_scaler_enabled`):

```python
#: Value-payoff effect classes that make a death trigger an aristocrats payoff
#: (as opposed to a non-value death trigger). Consumed by
#: ``_bulk_load_aristocrats_death_payoff_cards``.
_DEATH_VALUE_EFFECTS = frozenset(
    {"LoseLife", "GainLife", "DealDamage", "DamageAll", "PutCounter", "PutCounterAll", "Token", "Draw", "Mana"}
)


def _aristocrats_death_bridge_enabled() -> bool:
    """Read the ``aristocrats_death_bridge`` flag at call time (lazy import
    avoids a module-load cycle with ``complement_rules.aristocrats``)."""
    from .complement_rules import aristocrats

    return aristocrats._ENABLE_ARISTOCRATS_DEATH_BRIDGE


def _bulk_load_aristocrats_death_payoff_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Tier-1 ``death_payoff`` pool (~218): a ``trigger`` on a creature dying
    (``ChangesZone`` Battlefield->Graveyard, or ``Sacrificed``/``Dies``) whose
    ``execute_ref`` matches a same-card ``effect`` port's ``source_svar`` with a
    value ``event_class`` in :data:`_DEATH_VALUE_EFFECTS`, watching a Creature
    dying with scope broader than pure ``Card.Self``, excluding opponent-only
    triggers. All three conditions are required — dropping the Creature-scope
    condition reintroduces an ~696-card flood.
    Commander-independent; consumed by
    ``complement_rules.aristocrats._find_aristocrats_death_bridge``."""
    ph = ",".join("?" * len(_DEATH_VALUE_EFFECTS))
    rows = conn.execute(
        "SELECT DISTINCT t.card_name FROM card_ports t "
        "JOIN card_ports e ON e.card_name = t.card_name AND e.source_svar = t.execute_ref "
        "WHERE t.port_type = 'trigger' "
        "AND ( (t.event_class = 'ChangesZone' AND t.zone_origin = 'Battlefield' "
        "       AND t.zone_destination = 'Graveyard') "
        "     OR t.event_class IN ('Sacrificed', 'Dies') ) "
        "AND t.execute_ref IS NOT NULL AND t.execute_ref != '' "
        "AND e.port_type = 'effect' AND e.event_class IN (" + ph + ") "  # noqa: S608 — ph is placeholders, values bound below
        "AND t.valid_filter LIKE '%Creature%' "
        "AND t.valid_filter NOT IN ('Card.Self', 'Creature.Self') "
        "AND NOT (t.valid_filter LIKE '%OppCtrl%' AND t.valid_filter NOT LIKE '%YouCtrl%' "
        "         AND t.valid_filter NOT LIKE '%.Other%')",
        tuple(sorted(_DEATH_VALUE_EFFECTS)),
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


def _bulk_load_aristocrats_recursive_fodder_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Tier-2 ``recursive_fodder`` pool (~131): a Creature that returns ITSELF —
    ``Undying``/``Persist`` keyword, or a grave->bf ``ChangeZone`` effect whose
    ``valid_filter`` is empty / ``Card.Self`` / ``CARDNAME`` (self-recursion).
    The self-filter condition excludes reanimator value-engines that return
    OTHER creatures (Sun Titan, Reveillark). Commander-independent; consumed by
    ``complement_rules.aristocrats._find_aristocrats_death_bridge``."""
    rows = conn.execute(
        "SELECT DISTINCT p.card_name FROM card_ports p "
        "JOIN cards c ON c.name = p.card_name "
        "WHERE c.card_types LIKE '%Creature%' AND ( "
        "  p.granted_keyword IN ('Undying', 'Persist') "
        "  OR ( p.port_type = 'effect' AND p.event_class = 'ChangeZone' "
        "       AND p.zone_origin = 'Graveyard' AND p.zone_destination = 'Battlefield' "
        "       AND ( p.valid_filter IS NULL OR p.valid_filter = '' "
        "             OR p.valid_filter LIKE '%Card.Self%' OR p.valid_filter LIKE '%CARDNAME%' ) ) )"
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)
```

Add two fields at the END of the `CandidateCache` class body (after `x_cost_cost_reduce_cards`):

```python
    #: Aristocrats death-bridge pools consumed by
    #: ``_find_aristocrats_death_bridge``: ``death_payoff`` (death-triggered
    #: value payoffs, tier 1) and ``recursive_fodder`` (self-returning bodies,
    #: tier 2).
    aristocrats_death_payoff_cards: frozenset[str] = frozenset()
    aristocrats_recursive_fodder_cards: frozenset[str] = frozenset()
```

Wire the skip-when-off load in `build_candidate_cache` (next to the `x_cost_on` block):

```python
    aristocrats_on = _aristocrats_death_bridge_enabled()
```
```python
        aristocrats_death_payoff_cards=_bulk_load_aristocrats_death_payoff_cards(conn) if aristocrats_on else frozenset(),
        aristocrats_recursive_fodder_cards=_bulk_load_aristocrats_recursive_fodder_cards(conn) if aristocrats_on else frozenset(),
```

- [ ] **Step 4: Add the emitter in `aristocrats.py`**

```python
def _find_aristocrats_death_bridge(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Aristocrats death-value payoffs for sacrifice/death-engine commanders.

    Fires when the commander passes ``_commander_is_aristocrats``. Emits two IDF
    tiers scanned strong-first with dedup so the stronger credit wins:
    ``death_payoff`` (death-triggered value payoffs, ~218) > ``recursive_fodder``
    (self-returning bodies, ~131). A bounded, mechanically-discriminated subset —
    NOT a flat creature/token-producer flood.
    """
    if not _ENABLE_ARISTOCRATS_DEATH_BRIDGE:
        return []
    if not _commander_is_aristocrats(cmdr_ports):
        return []

    if candidate_cache is not None:
        payoffs = candidate_cache.aristocrats_death_payoff_cards
        fodder = candidate_cache.aristocrats_recursive_fodder_cards
    else:
        from ..penalties import (
            _bulk_load_aristocrats_death_payoff_cards,
            _bulk_load_aristocrats_recursive_fodder_cards,
        )

        payoffs = _bulk_load_aristocrats_death_payoff_cards(conn)
        fodder = _bulk_load_aristocrats_recursive_fodder_cards(conn)

    results: list[PortComplement] = []
    seen: set[str] = set()

    def _emit(name: str, tier: str) -> None:
        if name in cmdr_set or name in seen:
            return
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="aristocrats_death_bridge",
                direction="synergy",
                candidate=name,
                cmdr_event="death_engine",
                cand_event=tier,
            )
        )

    for name in sorted(payoffs):
        _emit(name, "death_payoff")
    for name in sorted(fodder):
        _emit(name, "recursive_fodder")
    return results
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/complement_rules/test_aristocrats_death_bridge.py -q`
Expected: PASS (all Task 1 + Task 2 tests)

- [ ] **Step 6: Commit**

```bash
git add src/mtg_synergy_graph/complement_rules/aristocrats.py src/mtg_synergy_graph/penalties.py tests/complement_rules/test_aristocrats_death_bridge.py
git commit -m "feat(aristocrats): two-tier death-bridge emitter + skip-when-off pools"
```

---

### Task 3: Flag-off wiring (core + registry) + hash-neutrality proof

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/core.py`
- Modify: `src/mtg_synergy_graph/complement_rules/registry.py`
- Test: `tests/complement_rules/test_aristocrats_death_bridge.py` (extend)

**Interfaces:**
- Consumes: `_find_aristocrats_death_bridge` (Task 2).
- Produces: rule wired into `find_all_complements`; flag-aware `RuleGate("aristocrats_death_bridge", _aristocrats_death_bridge_gate)` in `registry.py`.

- [ ] **Step 1: Write the failing gate test**

Append to `tests/complement_rules/test_aristocrats_death_bridge.py`:

```python
from mtg_synergy_graph.complement_rules.registry import attributable_rules_for_port


def test_rule_gate_flag_aware():
    port = {"port_type": "trigger", "event_class": "ChangesZone",
            "zone_origin": "Battlefield", "zone_destination": "Graveyard",
            "cost_subtype": "", "cost_target": "", "valid_filter": ""}
    arm._ENABLE_ARISTOCRATS_DEATH_BRIDGE = False
    assert "aristocrats_death_bridge" not in attributable_rules_for_port(port)
    arm._ENABLE_ARISTOCRATS_DEATH_BRIDGE = True
    try:
        assert "aristocrats_death_bridge" in attributable_rules_for_port(port)
    finally:
        arm._ENABLE_ARISTOCRATS_DEATH_BRIDGE = False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/complement_rules/test_aristocrats_death_bridge.py::test_rule_gate_flag_aware -q`
Expected: FAIL (rule not registered)

- [ ] **Step 3: Wire the emitter into `core.py`**

Add to the late `from .density import (...)`-adjacent import block (after the `.death_outlet` import, ~line 1149):

```python
from .aristocrats import _find_aristocrats_death_bridge  # noqa: E402
```

Add the `out.extend(...)` immediately after the `_find_x_cost_scaler` extend (~line 1393):

```python
        out.extend(_find_aristocrats_death_bridge(conn, cmdr_ports, cmdr_set, candidate_cache))
```

- [ ] **Step 4: Add the flag-aware gate + registration in `registry.py`**

Add near `_x_cost_scaler_gate`:

```python
def _aristocrats_death_bridge_gate(port: PortRow) -> bool:
    """Coarse single-port shape for aristocrats_death_bridge coverage attribution.

    Flag-aware: reads ``aristocrats._ENABLE_ARISTOCRATS_DEATH_BRIDGE`` at CALL
    time so gap_report / rule_quality_gate see NO coverage while the rule is off.
    Coarse by design (attribution only) — the full gate + emitter live in
    ``complement_rules.aristocrats``. Matches either commander-gate port shape:
    a creature sacrifice outlet, or a Battlefield->Graveyard death trigger."""
    from . import aristocrats

    if not aristocrats._ENABLE_ARISTOCRATS_DEATH_BRIDGE:
        return False
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt == "cost" and ev == "sacrifice":
        sub = port.get("cost_subtype") or ""
        tgt = (port.get("cost_target") or "").strip()
        return "Creature" in sub and tgt != "self"
    if pt == "trigger" and ev == "ChangesZone":
        zo = port.get("zone_origin") or ""
        zd = port.get("zone_destination") or ""
        return "Battlefield" in zo and "Graveyard" in zd
    return False
```

Add to the `RuleGate(...)` registry list (next to `RuleGate("x_cost_scaler", _x_cost_scaler_gate)`):

```python
    RuleGate("aristocrats_death_bridge", _aristocrats_death_bridge_gate),
```

- [ ] **Step 5: Run the gate test + full rule test file**

Run: `uv run pytest tests/complement_rules/test_aristocrats_death_bridge.py -q`
Expected: PASS

- [ ] **Step 6: Prove hash-neutrality (the critical guard)**

Run: `uv run scripts/bench.py audit --expect-identity`
Expected: PASS — bitwise-identical scores; `config_hash` reported as `c770b664e626`.
Run: `uv run pytest tests/ -q`
Expected: PASS (full suite; the flag-off build must add zero behavior).

- [ ] **Step 7: Commit**

```bash
git add src/mtg_synergy_graph/complement_rules/core.py src/mtg_synergy_graph/complement_rules/registry.py tests/complement_rules/test_aristocrats_death_bridge.py
git commit -m "feat(aristocrats): flag-off wiring (core extend + flag-aware RuleGate); --expect-identity PASS"
```

---

### Task 4: Cohort predicate + fixture + noise band

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cohorts.py`
- Modify: `src/mtg_synergy_graph/bench/coverage_report.py`
- Create: `scripts/bootstrap_aristocrats_fixture.py`
- Create: `tests/fixtures/golden_set_aristocrats.json` (generated artifact)
- Modify: `tests/bench/test_fixture_freshness.py`
- Test: `tests/bench/test_aristocrats_cohort.py`

**Interfaces:**
- Consumes: `LEGAL_LEGENDARY_CREATURE_WHERE` (cohorts.py); the parameterized `bootstrap_archetype_payoff_fixture.main(cohort_fn, output_path)`.
- Produces: `bench.cohorts.aristocrats(conn) -> set[str]`; `_COHORT_DISPATCH["aristocrats"]`; `tests/fixtures/golden_set_aristocrats.json`; the measured noise band (recorded in CLAUDE.md at pin time).

- [ ] **Step 1: Write the failing cohort test**

```python
# tests/bench/test_aristocrats_cohort.py
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.cohorts import aristocrats
from mtg_synergy_graph.bench.coverage_report import _COHORT_DISPATCH

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "synergy.db"


def test_dispatch_registration():
    assert _COHORT_DISPATCH.get("aristocrats") is aristocrats


@pytest.mark.skipif(not LIVE_DB.exists(), reason="requires built data/synergy.db")
def test_aristocrats_cohort_membership():
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    members = aristocrats(conn)
    # Sacrifice-outlet and death-trigger commanders are IN.
    assert "Yawgmoth, Thran Physician" in members
    assert "Meren of Clan Nel Toth" in members
    assert "Teysa Karlov" in members
    # A commander with no sac-outlet / death trigger is OUT.
    assert "Azusa, Lost but Seeking" not in members
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/bench/test_aristocrats_cohort.py -q`
Expected: FAIL — `ImportError: cannot import name 'aristocrats'`

- [ ] **Step 3: Add the cohort predicate in `cohorts.py`**

```python
def aristocrats(conn: sqlite3.Connection) -> set[str]:
    """Legal legendary-creature commanders that establish a death/sacrifice
    engine: a creature sacrifice outlet (``cost``/``sacrifice`` of a Creature,
    not self) OR a death-trigger payoff (``trigger``/``ChangesZone``
    Battlefield->Graveyard).

    Encodes the same condition as
    ``complement_rules.aristocrats._commander_is_aristocrats``. Target cohort of
    the ``aristocrats_death_bridge`` rule (spec 2026-07-09); deliberately NOT
    part of any shared cohort union. Measured cohort size 292.
    """
    rows = conn.execute(
        "SELECT DISTINCT c.name FROM cards c "  # noqa: S608 — no user input, LEGAL_LEGENDARY_CREATURE_WHERE is a module constant
        "WHERE " + LEGAL_LEGENDARY_CREATURE_WHERE + " AND c.name IN ("
        "  SELECT p.card_name FROM card_ports p WHERE "
        "    ( p.port_type = 'cost' AND p.event_class = 'sacrifice' "
        "      AND p.cost_subtype LIKE '%Creature%' "
        "      AND (p.cost_target IS NULL OR p.cost_target != 'self') ) "
        "    OR ( p.port_type = 'trigger' AND p.event_class = 'ChangesZone' "
        "         AND p.zone_origin = 'Battlefield' AND p.zone_destination = 'Graveyard' ) )"
    )
    return {row["name"] for row in rows}
```

- [ ] **Step 4: Register the cohort in `coverage_report.py`**

Add `aristocrats,` to the `from ...cohorts import (...)` block and `"aristocrats": aristocrats,` to `_COHORT_DISPATCH`.

- [ ] **Step 5: Create the bootstrap wrapper**

```python
# scripts/bootstrap_aristocrats_fixture.py
"""One-shot bootstrap: build the aristocrats cohort fixture.

Thin entry point over ``scripts/bootstrap_archetype_payoff_fixture.py``'s
parameterized build/pin protocol: selects ``bench.cohorts.aristocrats``
(sacrifice-outlet / death-trigger legal legendary-creature commanders), filters
to those with at least one ``High Synergy Cards`` row in EDHREC's tags.db, and
pins their top-N scores to ``tests/fixtures/golden_set_aristocrats.json``.

Evaluation instrument, zero scoring-path impact. Use THIS (not
``bench.py audit --repin``) after a cardsfolder import — ``--repin`` preserves
the old cohort_members snapshot. The fixture carries its own ``config_hash`` and
is enforced by ``tests/bench/test_fixture_freshness.py``.

Exit codes mirror the base module: 0 success, 2 if a required DB is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import bootstrap_archetype_payoff_fixture as _base

from mtg_synergy_graph.bench.cohorts import aristocrats

REPO_ROOT = _base.REPO_ROOT
OUTPUT_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_set_aristocrats.json"


def main() -> int:
    return _base.main(cohort_fn=aristocrats, output_path=OUTPUT_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Register the fixture in the freshness gate**

Add `"golden_set_aristocrats.json",` to `_COMMITTED_GOLDEN_FIXTURES` in `tests/bench/test_fixture_freshness.py`.

- [ ] **Step 7: Build and pin the fixture (flag OFF — pre-ship baseline)**

Run: `uv run python scripts/bootstrap_aristocrats_fixture.py`
Expected: writes `tests/fixtures/golden_set_aristocrats.json` at `config_hash c770b664e626`; prints the built/pinned commander count.

- [ ] **Step 8: Run cohort + freshness tests**

Run: `uv run pytest tests/bench/test_aristocrats_cohort.py tests/bench/test_fixture_freshness.py -q`
Expected: PASS

- [ ] **Step 9: Measure the noise band (flag OFF)**

Run this snippet and record the printed mean + half-width:

```bash
uv run python - <<'PY'
import sqlite3
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.bench.fixture import load_fixture
from mtg_synergy_graph.bench.per_commander_ndcg import compute_per_commander_ndcg_rows
from mtg_synergy_graph.bench.bands import bootstrap_band
conn = open_db("data/synergy.db", create=False)
edh = sqlite3.connect("data/tags.db"); edh.row_factory = sqlite3.Row
pinned = load_fixture("tests/fixtures/golden_set_aristocrats.json")
rows = compute_per_commander_ndcg_rows(conn, edh, pinned)
vals = [r.live_ndcg for r in rows if r.commander in pinned.cohort_members]
b = bootstrap_band(vals, seed=17)
print(f"cohort n={len(vals)} mean={b.mean:.4f} half_width={b.half_width:.4f}")
PY
```
(If a helper name differs, adapt to the actual `bench` API — the intent is: bootstrap band, seed 17, of `score_commander` top-30 NDCG@30 over the pinned cohort members, flag OFF. This half-width is the primary-gate threshold.)

- [ ] **Step 10: Record the band in CLAUDE.md**

Add an `aristocrats` cohort-fixture block to CLAUDE.md mirroring the `x_cost_scaler` block: build/pin/read commands + the measured band (mean, 95% CI, half-width, seed 17, n), noting it is the pre-ship (flag-OFF) baseline and must be recomputed after any data refresh or re-pin.

- [ ] **Step 11: Commit**

```bash
git add src/mtg_synergy_graph/bench/cohorts.py src/mtg_synergy_graph/bench/coverage_report.py \
        scripts/bootstrap_aristocrats_fixture.py tests/fixtures/golden_set_aristocrats.json \
        tests/bench/test_fixture_freshness.py tests/bench/test_aristocrats_cohort.py CLAUDE.md
git commit -m "feat(aristocrats): cohort predicate + pinned fixture + noise band"
```

---

### Task 5: Measurement — run the pre-registered gates

**Files:**
- No source changes. Produces a verdict and the `.audit` artifacts.

This task runs the flag-ON measurement in a throwaway process (the flag flip +
`1.5` multiplier injection stay in-process; the on-disk `config_hash` and all
pins remain valid at `c770b664e626`). Report each gate result verbatim; do not
backfill guards not reached because of a dispositive primary failure.

- [ ] **Step 1: Primary gate — in-cohort NDCG@30 uplift**

Measure the in-cohort mean ΔNDCG@30 with the flag ON and the `1.5` multiplier
injected in-process (`_RULE_QUALITY_MULTIPLIER["aristocrats_death_bridge"] = 1.5`),
against the flag-OFF pinned fixture. Compare the mean delta to the Task-4 noise
half-width.

- [ ] **Step 2: Anti-flood gate — tier mix / discrimination**

For the cohort, report per-commander tier composition (`death_payoff` vs
`recursive_fodder` counts in the top-30) and the top-30 contribution spread —
confirm credit is the specific death-value class, not a flat 30-body pool.

- [ ] **Step 3: Guard — golden-500 partitioned no-regression**

Run against `tests/fixtures/golden_set_run_500.json`: aggregate within noise;
confirm out-of-cohort collateral = 0 (the flag-aware gate fires only on
aristocrats ports).

- [ ] **Step 4: Guard — collinearity**

Run: `uv run scripts/bench.py audit --collinearity` (flag ON, in-process) and
confirm `aristocrats_death_bridge` is not near-collinear with `cost_feeds_trigger`,
the resonance family, `token_etb_damage`, or the sacrifice rules.

- [ ] **Step 5: Guard — rule_quality_gate**

Run: `uv run python scripts/rule_quality_gate.py --rule aristocrats_death_bridge`
(flag ON, in-process). Expected: PASS.

- [ ] **Step 6: Guard — hidden_gem no-regression**

Confirm `hidden_gem_hit_rate` Δ ≥ −0.02.

- [ ] **Step 7: Guard — whitelist-equivalence**

Show the death-value class is a genuine mechanical predicate that generalizes
beyond the pinned cohort members (it is defined by port shape, not by the
commander selection predicate) — i.e. the rule is not a disguised whitelist.

- [ ] **Step 8: Record the verdict**

Write the gate table + verdict to the progress ledger. **Decision rule:** SHIP
only if gate 1 clears the noise half-width AND gates 2–7 all pass. Then proceed
to Task 6 (SHIP) OR Task 7 (DECLINE) — never both.

---

### Task 6: SHIP path (only if Task 5 verdict = SHIP)

**Mutually exclusive with Task 7.** Skip entirely on a DECLINE verdict.

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/aristocrats.py` (flag → `True`)
- Modify: `src/mtg_synergy_graph/data/scoring_weights.json` (add `1.5` multiplier)
- Modify: all pinned fixtures (re-pin)
- Modify: `docs/RULE_HISTORY.md`, CLAUDE.md

- [ ] **Step 1:** Flip `_ENABLE_ARISTOCRATS_DEATH_BRIDGE = True`.
- [ ] **Step 2:** Add the `aristocrats_death_bridge` entry (`value: 1.5`) to `_RULE_QUALITY_MULTIPLIER` in `scoring_weights.json` (this flips `config_hash`).
- [ ] **Step 3:** Re-pin every committed fixture: `uv run scripts/bench.py audit --repin --yes` and each cohort fixture via its bootstrap script; run `uv run python scripts/bootstrap_aristocrats_fixture.py` (re-derives membership).
- [ ] **Step 4:** Recompute the aristocrats noise band at the new `config_hash` and update CLAUDE.md.
- [ ] **Step 5:** Add a RULE_HISTORY SHIP entry (date, gate results, per-commander impact).
- [ ] **Step 6:** Run `uv run pytest tests/ -q` (all green) and commit.

---

### Task 7: DECLINE path (only if Task 5 verdict = DECLINE)

**Mutually exclusive with Task 6.** The working tree stays flag-off, hash-neutral.

**Files:**
- Create: `docs/solutions/best-practices/aristocrats-death-bridge-null-result-2026-07-09.md`
- Modify: `docs/RULE_HISTORY.md`

- [ ] **Step 1:** Write the null-result doc: the exact gate results, root-cause analysis (which cohort members gained vs regressed, and why), and — if the primary failed despite the tight 218/131 pools — what that says about the death-value class's EDHREC alignment. Link `[[calibration-levers-exhausted-2026-07-09]]` and the three prior null-results.
- [ ] **Step 2:** Add a RULE_HISTORY DECLINE entry under `## 2026-07-09`.
- [ ] **Step 3:** Confirm `uv run scripts/bench.py audit --expect-identity` PASS and `config_hash` still `c770b664e626`; run `uv run pytest tests/ -q`; commit.

---

## Self-Review

- **Spec coverage:** Units 1–4 of the spec map to Tasks 1–4; the six gates + whitelist check map to Task 5; the SHIP/DECLINE fork maps to Tasks 6/7. ✓
- **Type consistency:** `_find_aristocrats_death_bridge(conn, cmdr_ports, cmdr_set, candidate_cache=None)` signature matches the `core.py` `out.extend(...)` call and the `_find_x_cost_scaler` template; `PortComplement` fields (`rule_id`, `direction`, `candidate`, `cmdr_event`, `cand_event`) match the template. ✓
- **No placeholders:** every code step carries the actual code; the noise-band snippet flags the one API name to verify against the live `bench` module. ✓
- **Hash-neutrality:** asserted at Task 3 Step 6 and Task 7 Step 3; the flag is never added to `ScoringConfigInputs`; the `scoring_weights.json` entry lands only at Task 6 (SHIP). ✓
