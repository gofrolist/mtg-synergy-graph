# Subtype-Supply Rule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**status:** shipped (PARTIAL band, human-approved)
**plan id:** 2026-07-07-001
**evidence base:** docs/solutions/best-practices/deck-context-null-result-2026-07-06.md (Whitelist Finding), docs/plans/2026-07-03-001-feat-archetype-payoff-cohort-fixture-plan.md (binding obligations), docs/plans/2026-07-06-001-feat-structural-gap-remediation-plan.md (Phase A DECLINE that produced the finding)

**Goal:** Ship a narrow, IDF-weighted subtype-supply complement rule — commanders with a subtype-keyed death-payoff trigger (Slimefoot, Wilhelt, …) score candidates that *produce tokens of* or *are bodies of* the payoff subtype — and accept it only if it beats both hardcoded-whitelist variants' pinned numbers without their gem/cliff bill.

**Architecture:** One new Python rule helper (`complement_rules/subtype_supply.py`) emitting two rule_ids (`subtype_supply_producer`, `subtype_supply_body`) with independent quality multipliers, gated by a module flag `_ENABLE_SUBTYPE_SUPPLY` (default **False** — zero scoring-path change until the decision gates pass). Commander-side detection reuses the exact archetype-payoff cohort predicate via a new shared module `death_payoff.py` (extracted from `bench/cohorts.py` so the scoring path never imports from `bench/`). The declarative-row route named in the Whitelist Finding is **structurally impossible**: the interpreter's `GATE_OPS` leaf ops take only fixed literals, and `commander_port_predicate` is stored but never consumed (`port_graph/interpreter.py:19-23`) — a parameterized "extract subtype X from the commander, match candidates on X" join requires the Python escape hatch (`docs/RULE_PLANNING.md` FR6).

**Tech Stack:** Python 3 / sqlite3, existing complement-rule machinery (`PortComplement`, IDF via `(rule_id, cmdr_event, cand_event, filter_group)` keys), standing instruments `scripts/context_sim.py` + `scripts/bench.py audit`.

## Global Constraints

Binding numbers — every task's requirements implicitly include this section.

**Pinned baselines (recorded 2026-07-06, `.audit/context_sim/PINNED_GATES.md`):**
- Cohort fixture (`tests/fixtures/golden_set_archetype_payoff.json`, n=33, page-based): NDCG mean **0.2858**, half-width **H_cohort = 0.0567**; gem mean 0.9020, gem half-width **0.0242**.
- Golden-500 (`tests/fixtures/golden_set_run_500.json`, page-based): NDCG mean **0.1531**, half-width **H_500 = 0.0136**; gem mean 0.8189, gem half-width **G_500 = 0.0235**.

**Whitelist bars to beat (G4 comparators, measured through the same page-based assembly; `.audit/context_sim/whitelist_cohort{,_v2}/whitelist.md`):**

| variant | bonus | ΔNDCG | cliffs | gemΔ |
|---|---|---|---|---|
| producer-only | 0.10 | +0.0191 | 0 | −0.0061 |
| producer-only | 0.25 | +0.0531 | 1 | −0.0455 |
| producer-only | 0.50 | **+0.0697** | 6 | −0.1222 |
| full (bodies+producers) | 0.10 | +0.0147 | 1 | −0.0030 |
| full | 0.25 | +0.0376 | 2 | −0.0162 |
| full | 0.50 | +0.0523 | 5 | −0.0586 |

**Decision gates (all measured at the chosen operating point after Task 5 tuning):**
- **S1 (beat the whitelist):** cohort ΔNDCG ≥ **+0.0697** (best producer-only cell — the tougher variant).
- **S2 (don't pay with gems):** cohort gem Δ ≥ **−0.0242** (within the cohort gem noise band; contrast the whitelist's −0.0455/−0.1222 at its best NDCG cells).
- **S3 (no head-shredding):** cohort per-commander cliffs (ΔNDCG@30 < −0.05 vs baseline) ≤ **1**.
- **S4 (golden-500 no-regression):** golden-500 ΔNDCG ≥ **−0.0136** AND golden-500 gem Δ ≥ **−0.0235**.
- **S5 (golden-100 audit health):** `bench.py audit` histogram verdict non-NEGATIVE; hidden-gem stderr warning (aggregate gem delta < −0.02) must not fire.
- **S6 (rule quality / redundancy):** `rule_quality_gate.py` PASS for both rule_ids; `bench.py audit --collinearity` shows no pair with |r|>0.8 AND VIF>5 against existing rules (check `tribal_density`, `token_producer`, `dies_drain`, `effect_feeds_trigger`, `creature_died_feeder` explicitly).
- **PARTIAL** (escalate to human with the full table, do not ship or decline unilaterally): S2–S6 pass and cohort ΔNDCG ∈ [+0.0567, +0.0697).
- **DECLINE** otherwise: flag stays False (zero scoring-path change), write the null-result doc (Task 6), keep code + tests + helpers as standing infra.

**Measurement-path discipline (binding):** the whitelist bars were measured via `context_sim`'s page-based assembly against the w=0 pin. The rule's cohort/golden-500 deltas MUST be measured the same way: `scripts/context_sim.py bands` live means vs the recorded 0.2858 / 0.1531 (a `context_sim bands` run with the flag ON *is* plain production ranking — the instrument's w=0 self-check guarantees it equals `engine.page()`). The `bench.py audit --per-commander-ndcg` in-cohort readout (reporter band mean 0.1436, half-width 0.0448, per CLAUDE.md) is a **sidecar only** — different instrument, do not gate on it or compare its numbers to the page-based bars.

**Necessary-but-not-sufficient caveat (from `bench/cohorts.py` docstring):** the cohort is selected by the same predicate this rule keys on, so a cohort gain alone proves nothing — that is exactly why S1/S2/S3 are pinned to the *whitelist's* numbers and S4/S5 to the whole-fixture bands.

**Repo conventions:** `uv run` for everything; tests NEVER pass repo-relative DB paths (use `tmp_path` / in-memory); subtype matching is exact-token on split (`cards.subtypes` is space-separated; `LIKE '%Rat%'` matches Pirate — documented bug, `context_sim.py:379-387`); commit messages end with the two standard trailers; run `graphify update .` after code changes; `.audit/` is gitignored.

**Config-hash choreography (why the flag gates *registration inputs*, not just behavior):** `compute_config_hash` folds `scoring_weights.json` values (`bench/tensor.py:69-70`). Tasks 1–3 therefore add NO `scoring_weights.json` entries and keep the flag False → hash unchanged → `bench.py audit --expect-identity` must PASS at the Task 3 commit. Task 4 flips the flag and adds the weight entries in the working tree (uncommitted) for measurement; only the Task 6 SHIP path commits the flip + re-pins all fixtures.

---

## Task 1: Shared `death_payoff` module (extract cohort predicate helpers)

The rule needs `_is_death_event` / `_valid_filter_subtype_tokens` / `_token_subtype_vocab`, which live in `src/mtg_synergy_graph/bench/cohorts.py`. The scoring path must not import from `bench/`, so extract them to a new top-level module and have `cohorts.py` re-import under its old private names (so `bench/context_sim.py`'s `from .cohorts import _is_death_event, ...` keeps working untouched).

**Files:**
- Create: `src/mtg_synergy_graph/death_payoff.py`
- Modify: `src/mtg_synergy_graph/bench/cohorts.py`
- Test: `tests/test_death_payoff.py`

**Interfaces:**
- Produces: `is_death_event(event_class, zone_origin, zone_destination) -> bool`, `valid_filter_subtype_tokens(valid_filter) -> list[str]`, `token_subtype_vocab(conn) -> set[str]`, `payoff_subtypes_from_ports(conn, cmdr_ports) -> list[str]` (sorted). Task 2 consumes `payoff_subtypes_from_ports`.
- Invariant: `bench.cohorts.archetype_payoff_cohort(conn)` output is bitwise-unchanged (the fixture's pinned `cohort_members` snapshot is the oracle).

- [ ] **Step 1: Write the failing test**

```python
"""Tests for mtg_synergy_graph.death_payoff (plan 2026-07-07-001 Task 1)."""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.death_payoff import (
    is_death_event,
    payoff_subtypes_from_ports,
    token_subtype_vocab,
    valid_filter_subtype_tokens,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name TEXT NOT NULL,
            port_type TEXT NOT NULL,
            event_class TEXT NOT NULL,
            valid_filter TEXT,
            zone_origin TEXT,
            zone_destination TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE port_attributes (
            port_id INTEGER NOT NULL,
            attr_kind TEXT NOT NULL,
            attr_value TEXT NOT NULL,
            is_negated BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (port_id, attr_kind, attr_value, is_negated)
        )
        """
    )
    return conn


@pytest.fixture()
def conn():
    c = _make_db()
    yield c
    c.close()


def _add_port(conn, card, port_type, event_class, valid_filter=None, zo=None, zd=None) -> int:
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (card, port_type, event_class, valid_filter, zo, zd),
    )
    return cur.lastrowid


def _add_token_subtype(conn, port_id, subtype):
    conn.execute(
        "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', ?)",
        (port_id, subtype),
    )


class TestIsDeathEvent:
    def test_sacrificed_is_unconditional(self):
        assert is_death_event("Sacrificed", None, None) is True
        assert is_death_event("SacrificedOnce", "Library", "Exile") is True

    def test_changeszone_needs_graveyard_from_battlefield(self):
        assert is_death_event("ChangesZone", "Battlefield", "Graveyard") is True
        assert is_death_event("ChangesZoneAll", "", "Graveyard") is True
        assert is_death_event("ChangesZone", "Library", "Graveyard") is False  # mill
        assert is_death_event("ChangesZone", "Battlefield", "Exile") is False

    def test_other_events_never_match(self):
        assert is_death_event("SpellCast", None, None) is False


class TestValidFilterSubtypeTokens:
    def test_head_and_restriction_forms(self):
        assert valid_filter_subtype_tokens("Insect.YouCtrl,Creature.Zombie+Other") == [
            "Insect",
            "YouCtrl",
            "Creature",
            "Zombie",
            "Other",
        ]

    def test_negated_tokens_keep_prefix(self):
        assert "!Zombie" in valid_filter_subtype_tokens("Creature.!Zombie")


class TestPayoffSubtypesFromPorts:
    def test_extracts_vocab_intersected_subtype(self, conn):
        pid = _add_port(conn, "Some Producer", "effect", "Token")
        _add_token_subtype(conn, pid, "Saproling")
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "ChangesZone",
                "valid_filter": "Saproling.YouCtrl",
                "zone_origin": "Battlefield",
                "zone_destination": "Graveyard",
            }
        ]
        assert payoff_subtypes_from_ports(conn, cmdr_ports) == ["Saproling"]

    def test_non_death_trigger_yields_nothing(self, conn):
        pid = _add_port(conn, "Some Producer", "effect", "Token")
        _add_token_subtype(conn, pid, "Saproling")
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "ChangesZone",
                "valid_filter": "Saproling.YouCtrl",
                "zone_origin": "Any",
                "zone_destination": "Battlefield",  # ETB, not a death
            }
        ]
        assert payoff_subtypes_from_ports(conn, cmdr_ports) == []

    def test_subtype_outside_vocab_rejected(self, conn):
        # vocab is empty -> nothing can match
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "Sacrificed",
                "valid_filter": "Saproling.YouCtrl",
                "zone_origin": None,
                "zone_destination": None,
            }
        ]
        assert payoff_subtypes_from_ports(conn, cmdr_ports) == []

    def test_vocab_reads_token_subtype_rows(self, conn):
        pid = _add_port(conn, "Some Producer", "effect", "Token")
        _add_token_subtype(conn, pid, "Zombie")
        assert token_subtype_vocab(conn) == {"Zombie"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_death_payoff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_synergy_graph.death_payoff'`

- [ ] **Step 3: Create the module by MOVING (not copying) the helpers**

Create `src/mtg_synergy_graph/death_payoff.py`. Move verbatim from `bench/cohorts.py`: `_SACRIFICE_EVENTS`, `_CHANGESZONE_EVENTS`, `_BATTLEFIELD_TOLERANT_ORIGINS` (keep names), and the four functions renamed public: `_token_subtype_vocab`→`token_subtype_vocab`, `_valid_filter_subtype_tokens`→`valid_filter_subtype_tokens`, `_reaches_graveyard_from_battlefield`→`reaches_graveyard_from_battlefield`, `_is_death_event`→`is_death_event`. Keep every docstring and comment (they encode audit history). Module docstring:

```python
"""Death-payoff subtype detection — shared by the cohort predicate and the
subtype-supply rule (plan 2026-07-07-001).

Extracted verbatim from ``bench/cohorts.py`` so the scoring path
(``complement_rules/subtype_supply.py``) can reuse the EXACT predicate that
selects the archetype-payoff cohort without importing from ``bench``.
Behavior change here changes BOTH the cohort membership and the rule gate —
the cohort fixture's pinned ``cohort_members`` snapshot is the regression
oracle (tests/test_death_payoff.py::TestCohortUnchanged).
"""
```

Then append the new composition:

```python
def payoff_subtypes_from_ports(conn: sqlite3.Connection, cmdr_ports: list) -> list[str]:
    """Sorted payoff subtypes named by the commander's death-trigger filters.

    A subtype qualifies when (a) some trigger port is a death event
    (:func:`is_death_event`) and (b) its ``valid_filter`` names a token in
    the token-producible vocabulary (:func:`token_subtype_vocab`) — the same
    two conditions as ``bench.cohorts.subtype_death_payoff``, applied to an
    already-loaded port list instead of a DB-wide scan.
    """
    vocab = token_subtype_vocab(conn)
    if not vocab:
        return []
    subs: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        event_class = (p.get("event_class") or "").strip()
        if not is_death_event(event_class, p.get("zone_origin"), p.get("zone_destination")):
            continue
        valid_filter = p.get("valid_filter") or ""
        if not valid_filter:
            continue
        subs.update(t for t in valid_filter_subtype_tokens(valid_filter) if t in vocab)
    return sorted(subs)
```

In `bench/cohorts.py`, delete the moved code and re-import under the old private names so `bench/context_sim.py` is untouched:

```python
from mtg_synergy_graph.death_payoff import (
    is_death_event as _is_death_event,
    token_subtype_vocab as _token_subtype_vocab,
    valid_filter_subtype_tokens as _valid_filter_subtype_tokens,
)
```

(`subtype_death_payoff` and `archetype_payoff_cohort` stay in `cohorts.py`, now calling the imported helpers. `_reaches_graveyard_from_battlefield` has no external importers — verify with grep — so it needs no alias.)

- [ ] **Step 4: Add the cohort-unchanged regression test** (append to `tests/test_death_payoff.py`)

```python
import json
import os
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "golden_set_archetype_payoff.json"
LIVE_DB = Path(__file__).resolve().parents[1] / "data" / "synergy.db"


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
class TestCohortUnchanged:
    def test_membership_matches_pinned_snapshot(self):
        """The refactor must not move cohort membership by one card."""
        from mtg_synergy_graph.bench.cohorts import archetype_payoff_cohort
        from mtg_synergy_graph.db import open_db

        pinned = set(json.loads(FIXTURE.read_text())["cohort_members"])
        conn = open_db(str(LIVE_DB), create=False)
        try:
            live = archetype_payoff_cohort(conn)
        finally:
            conn.close()
        assert live == pinned
```

(If the fixture's JSON key is not literally `cohort_members`, read the fixture first and use its actual key — do not guess; the bootstrap script `scripts/bootstrap_archetype_payoff_fixture.py` shows the schema.)

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_death_payoff.py tests/bench/ -q`
Expected: PASS (new tests + all existing bench tests, including the fixture freshness gate and any existing cohorts tests).

- [ ] **Step 6: Full suite + commit**

Run: `uv run pytest tests/ -q` — expected all pass.
Run: `graphify update .`

```bash
git add src/mtg_synergy_graph/death_payoff.py src/mtg_synergy_graph/bench/cohorts.py tests/test_death_payoff.py graphify-out
git commit -m "refactor: extract death-payoff subtype helpers to shared module

Scoring-path-importable home for the cohort predicate helpers, ahead of
the subtype-supply rule (plan 2026-07-07-001 Task 1). Cohort membership
verified unchanged against the pinned fixture snapshot."
```

(Append the two standard trailers to every commit in this plan.)

---

## Task 2: Rule helper + TDD tests (no wiring — pure function, flag default False)

**Files:**
- Create: `src/mtg_synergy_graph/complement_rules/subtype_supply.py`
- Test: `tests/test_subtype_supply.py`

**Interfaces:**
- Consumes: `death_payoff.payoff_subtypes_from_ports` (Task 1), `core.PortComplement`.
- Produces: `_find_subtype_supply_complements(conn, cmdr_ports, cmdr_set) -> list[PortComplement]` and module flag `_ENABLE_SUBTYPE_SUPPLY: bool = False`. Task 3 wires both.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for complement_rules.subtype_supply (plan 2026-07-07-001 Task 2).

RULE_PLANNING.md section 4 required cases: gate rejection, qualifier
rejection, per-direction match, per-direction exclusion, dedup, commander
self-exclusion, exact rule_id.
"""

from __future__ import annotations

import sqlite3

import pytest

import mtg_synergy_graph.complement_rules.subtype_supply as ss
from mtg_synergy_graph.complement_rules.subtype_supply import (
    _find_subtype_supply_complements,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE cards (
            name TEXT PRIMARY KEY,
            subtypes TEXT,
            card_types TEXT,
            edhrec_rank INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name TEXT NOT NULL,
            port_type TEXT NOT NULL,
            event_class TEXT NOT NULL,
            valid_filter TEXT,
            zone_origin TEXT,
            zone_destination TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE port_attributes (
            port_id INTEGER NOT NULL,
            attr_kind TEXT NOT NULL,
            attr_value TEXT NOT NULL,
            is_negated BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (port_id, attr_kind, attr_value, is_negated)
        )
        """
    )
    return conn


def _add_card(conn, name, subtypes="", card_types="Creature"):
    conn.execute(
        "INSERT INTO cards (name, subtypes, card_types) VALUES (?, ?, ?)",
        (name, subtypes, card_types),
    )


def _add_producer(conn, name, subtype):
    _add_card(conn, name)
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, 'effect', 'Token')",
        (name,),
    )
    conn.execute(
        "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', ?)",
        (cur.lastrowid, subtype),
    )


DEATH_TRIGGER = {
    "port_type": "trigger",
    "event_class": "ChangesZone",
    "valid_filter": "Saproling.YouCtrl",
    "zone_origin": "Battlefield",
    "zone_destination": "Graveyard",
}


@pytest.fixture()
def conn():
    c = _make_db()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """Rule logic tests run with the flag ON; the default-off contract has
    its own explicit test below."""
    monkeypatch.setattr(ss, "_ENABLE_SUBTYPE_SUPPLY", True)


class TestFindSubtypeSupply:
    def test_flag_off_returns_nothing(self, conn, monkeypatch):
        monkeypatch.setattr(ss, "_ENABLE_SUBTYPE_SUPPLY", False)
        _add_producer(conn, "Sprout Swarm", "Saproling")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        assert out == []

    def test_gate_rejects_commander_without_death_trigger(self, conn):
        _add_producer(conn, "Sprout Swarm", "Saproling")
        etb = dict(DEATH_TRIGGER, zone_origin="Any", zone_destination="Battlefield")
        assert _find_subtype_supply_complements(conn, [etb], {"Slimefoot"}) == []

    def test_gate_rejects_subtype_outside_token_vocab(self, conn):
        # No port_attributes rows at all -> empty vocab -> no payoff subtype.
        _add_card(conn, "Some Body", subtypes="Saproling")
        assert _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"}) == []

    def test_producer_direction_matches(self, conn):
        _add_producer(conn, "Sprout Swarm", "Saproling")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        producers = [c for c in out if c.rule_id == "subtype_supply_producer"]
        assert [c.candidate for c in producers] == ["Sprout Swarm"]
        assert producers[0].direction == "synergy"
        assert producers[0].cmdr_event == "death_payoff"
        assert producers[0].cand_event == "Saproling"

    def test_producer_direction_excludes_other_subtypes(self, conn):
        _add_producer(conn, "Sprout Swarm", "Saproling")  # establishes vocab
        _add_producer(conn, "Krenko", "Goblin")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        names = {c.candidate for c in out if c.rule_id == "subtype_supply_producer"}
        assert names == {"Sprout Swarm"}

    def test_body_direction_matches_exact_token(self, conn):
        _add_producer(conn, "Sprout Swarm", "Saproling")  # establishes vocab
        _add_card(conn, "Mycoloth", subtypes="Fungus Saproling")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        bodies = {c.candidate for c in out if c.rule_id == "subtype_supply_body"}
        assert "Mycoloth" in bodies

    def test_body_direction_is_token_anchored_not_substring(self, conn):
        """The documented Rat-in-Pirate bug: subtype match must split, not LIKE."""
        rat_trigger = dict(DEATH_TRIGGER, valid_filter="Rat.YouCtrl")
        _add_producer(conn, "Rat Producer", "Rat")  # establishes Rat in vocab
        _add_card(conn, "Ruthless Knave", subtypes="Human Pirate")
        out = _find_subtype_supply_complements(conn, [rat_trigger], {"Marrow-Gnawer"})
        bodies = {c.candidate for c in out if c.rule_id == "subtype_supply_body"}
        assert "Ruthless Knave" not in bodies

    def test_dedup_one_complement_per_card_per_rule(self, conn):
        # Card both produces Saproling tokens twice -> still one producer row.
        _add_producer(conn, "Sprout Swarm", "Saproling")
        cur = conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Sprout Swarm', 'effect', 'Token')"
        )
        conn.execute(
            "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', 'Saproling')",
            (cur.lastrowid,),
        )
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        producers = [c for c in out if c.rule_id == "subtype_supply_producer"]
        assert len(producers) == 1

    def test_card_matching_both_directions_gets_both_rule_ids(self, conn):
        _add_producer(conn, "Tender Greenkeeper", "Saproling")
        conn.execute(
            "UPDATE cards SET subtypes = 'Elf Druid Saproling' WHERE name = 'Tender Greenkeeper'"
        )
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        rule_ids = {c.rule_id for c in out if c.candidate == "Tender Greenkeeper"}
        assert rule_ids == {"subtype_supply_producer", "subtype_supply_body"}

    def test_commander_self_exclusion(self, conn):
        _add_producer(conn, "Slimefoot, the Stowaway", "Saproling")
        out = _find_subtype_supply_complements(
            conn, [DEATH_TRIGGER], {"Slimefoot, the Stowaway"}
        )
        assert out == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_subtype_supply.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_synergy_graph.complement_rules.subtype_supply'`

- [ ] **Step 3: Implement**

Create `src/mtg_synergy_graph/complement_rules/subtype_supply.py`:

```python
"""Subtype-supply complement rules (plan 2026-07-07-001).

Commander gate: a subtype-keyed death-payoff trigger — the EXACT predicate
that selects the archetype-payoff cohort (via
``death_payoff.payoff_subtypes_from_ports``). Two candidate directions with
independent rule_ids so their quality multipliers tune independently
(whitelist evidence: flooding bodies dilutes; producers are the tougher,
cleaner signal — see deck-context-null-result-2026-07-06.md):

- ``subtype_supply_producer`` — candidate has a port that produces tokens of
  the payoff subtype (``port_attributes`` ``attr_kind='token_subtype'``).
- ``subtype_supply_body`` — candidate's ``cards.subtypes`` contains the
  payoff subtype (space-split exact-token membership, NOT LIKE — the
  documented Rat-substring-of-Pirate bug).

Both are IDF-weighted like any other rule via the
``(rule_id, cmdr_event, cand_event, filter_group)`` key with
``cand_event=<subtype>``, so rare payoff subtypes (Saproling) weigh more
than common ones (Zombie) automatically. NOT a flat bonus.

Flag-gated default-OFF until the plan's decision gates pass (S1-S6);
registration inputs (scoring_weights entries) land only on the SHIP path so
the Task 3 commit is config-hash-neutral.
"""

from __future__ import annotations

import sqlite3

from mtg_synergy_graph.death_payoff import payoff_subtypes_from_ports

from .core import PortComplement, PortRow

#: Decision-gated (plan 2026-07-07-001). Flip to True only on the SHIP path
#: (gates S1-S6), together with the scoring_weights.json entries + re-pin.
_ENABLE_SUBTYPE_SUPPLY = False


def _find_subtype_supply_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Producer + body supply for subtype-keyed death-payoff commanders."""
    if not _ENABLE_SUBTYPE_SUPPLY:
        return []
    subs = payoff_subtypes_from_ports(conn, cmdr_ports)
    if not subs:
        return []

    results: list[PortComplement] = []

    seen_producer: set[str] = set()
    for sub in subs:
        cur = conn.execute(
            "SELECT DISTINCT p.card_name FROM card_ports p "
            "JOIN port_attributes a ON a.port_id = p.id "
            "WHERE a.attr_kind = 'token_subtype' AND a.attr_value = ?",
            (sub,),
        )
        for r in cur.fetchall():
            name = r["card_name"]
            if name in cmdr_set or name in seen_producer:
                continue
            seen_producer.add(name)
            results.append(
                PortComplement(
                    rule_id="subtype_supply_producer",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="death_payoff",
                    cand_event=sub,
                )
            )

    sub_set = set(subs)
    seen_body: set[str] = set()
    cur = conn.execute("SELECT name, subtypes FROM cards WHERE subtypes IS NOT NULL AND subtypes != ''")
    for r in cur.fetchall():
        name = r["name"]
        if name in cmdr_set or name in seen_body:
            continue
        matched = sub_set & set((r["subtypes"] or "").split())
        if not matched:
            continue
        seen_body.add(name)
        results.append(
            PortComplement(
                rule_id="subtype_supply_body",
                direction="synergy",
                candidate=name,
                cmdr_event="death_payoff",
                cand_event=min(matched),
            )
        )

    return results
```

(If `PortRow` is not importable from `.core`, import it from wherever `density.py` gets it — match that file's imports exactly.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_subtype_supply.py -v`
Expected: all PASS.

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest tests/ -q` — expected all pass (rule is unreferenced + flag-off; nothing changes).
Run: `graphify update .`

```bash
git add src/mtg_synergy_graph/complement_rules/subtype_supply.py tests/test_subtype_supply.py graphify-out
git commit -m "feat(rules): subtype-supply rule helper, flag-gated default-off

Two rule_ids (producer/body) keyed on the death-payoff cohort predicate.
Not yet wired; zero scoring-path impact (plan 2026-07-07-001 Task 2)."
```

---

## Task 3: Wiring (dispatch + registry), still flag-off, hash-neutral

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/core.py` (import + `_card_attr_complements()` dispatch)
- Modify: `src/mtg_synergy_graph/complement_rules/registry.py` (`CARD_LEVEL_RULES`)
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (`_RULE_TO_BUCKET`)
- Test: extend `tests/test_subtype_supply.py`

**Interfaces:**
- Consumes: `_find_subtype_supply_complements` (Task 2).
- Produces: rule reachable from `find_all_complements()` when the flag is True. NO `scoring_weights.json` entries in this task (config-hash-neutral — see Global Constraints).

- [ ] **Step 1: Write the failing integration test** (append to `tests/test_subtype_supply.py`)

```python
class TestWiring:
    def test_registered_in_card_level_rules(self):
        from mtg_synergy_graph.complement_rules.registry import CARD_LEVEL_RULES

        assert "subtype_supply_producer" in CARD_LEVEL_RULES
        assert "subtype_supply_body" in CARD_LEVEL_RULES

    def test_bucket_mapping(self):
        from mtg_synergy_graph.universal_scorer import _RULE_TO_BUCKET

        assert _RULE_TO_BUCKET["subtype_supply_producer"] == "port_match"
        assert _RULE_TO_BUCKET["subtype_supply_body"] == "port_match"

    def test_dispatched_from_core(self):
        """core.py must call the helper (source-level check keeps the test
        independent of a full engine fixture)."""
        import inspect

        from mtg_synergy_graph.complement_rules import core

        src = inspect.getsource(core)
        assert "_find_subtype_supply_complements" in src
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_subtype_supply.py::TestWiring -v`
Expected: FAIL (KeyError / assertion errors).

- [ ] **Step 3: Wire**

In `core.py`: add `from .subtype_supply import _find_subtype_supply_complements  # noqa: E402` beside the other rule imports, and inside `_card_attr_complements()` add, next to the `_find_token_producers_for_trigger` line:

```python
        out.extend(_find_subtype_supply_complements(conn, cmdr_ports, cmdr_set))
```

In `registry.py`, extend `CARD_LEVEL_RULES` (in the "conjunction of TWO distinct ports / card attributes" group):

```python
        # Subtype-supply (plan 2026-07-07-001): gate is a conjunction of a
        # death-shaped trigger port AND its valid_filter naming a
        # token-producible subtype; body direction matches cards.subtypes.
        "subtype_supply_producer",
        "subtype_supply_body",
```

In `universal_scorer.py`, add to `_RULE_TO_BUCKET`:

```python
    "subtype_supply_producer": "port_match",
    "subtype_supply_body": "port_match",
```

- [ ] **Step 4: Verify hash-neutrality and identity**

Run: `uv run pytest tests/ -q` — expected all pass (flag still False; freshness gates untouched).
Run: `uv run scripts/bench.py audit --expect-identity`
Expected: PASS — bitwise-identical scores, config hash unchanged (no scoring_weights entries were added). If this fails, something in the wiring leaks behavior with the flag off — fix before committing.

- [ ] **Step 5: Commit**

Run: `graphify update .`

```bash
git add src/mtg_synergy_graph/complement_rules/core.py src/mtg_synergy_graph/complement_rules/registry.py src/mtg_synergy_graph/universal_scorer.py tests/test_subtype_supply.py graphify-out
git commit -m "feat(rules): wire subtype-supply rule (flag-off, hash-neutral)

Dispatch + CARD_LEVEL_RULES + bucket mapping. --expect-identity PASS;
zero scoring-path change until the decision gates pass (plan 2026-07-07-001 Task 3)."
```

---

## Task 4: Measurement (working-tree flag ON — nothing committed)

All edits in this task stay **uncommitted**; the deliverable is `.audit/subtype_supply/decision.md` plus the task report. Working tree at start = Task 3 commit.

**Files:**
- Working-tree only: `complement_rules/subtype_supply.py` (`_ENABLE_SUBTYPE_SUPPLY = True`), `src/mtg_synergy_graph/data/scoring_weights.json` (add `_RULE_QUALITY_MULTIPLIER` entries: `subtype_supply_producer` value 2.5, `subtype_supply_body` value 1.0 — starting points; comments citing this plan)
- Create (gitignored): `.audit/subtype_supply/decision.md`

- [ ] **Step 1: Flip the flag + add starting weights (working tree)**

Edit `subtype_supply.py`: `_ENABLE_SUBTYPE_SUPPLY = True`. Edit `scoring_weights.json` `_RULE_QUALITY_MULTIPLIER`, alphabetical position:

```json
"subtype_supply_body": {"value": 1.0, "comment": "Plan 2026-07-07-001 starting point; tuned in Task 5. Body direction expected weaker (whitelist evidence: bodies dilute)."},
"subtype_supply_producer": {"value": 2.5, "comment": "Plan 2026-07-07-001 starting point; tuned in Task 5. Matches feeder-family prior (2.0-3.0)."}
```

- [ ] **Step 2: Golden-100 audit vs the pinned baseline**

Run: `uv run scripts/bench.py audit`
Record: verdict, aggregate delta, hidden_gem_hit_rate delta, per-commander winners/losers, whether the gem stderr warning fired. (This is the designed new-rule flow; the audit reports the live-vs-pin delta across the hash change.)

- [ ] **Step 3: Rule-quality + redundancy gates**

Run: `uv run python scripts/rule_quality_gate.py --rule subtype_supply_producer`
Run: `uv run python scripts/rule_quality_gate.py --rule subtype_supply_body`
Run: `uv run scripts/bench.py audit --collinearity` — record any pair involving the two new rule_ids; S6 fails on |r|>0.8 AND VIF>5.
Run: `uv run scripts/bench.py audit --rule subtype_supply_producer` and `--rule subtype_supply_body` (per-rule ablation summaries) and `--inspect subtype_supply_producer --limit 20` (sanity: top contributions should be Saproling/Zombie/etc. producers on cohort commanders, not generic staples).

- [ ] **Step 4: Cohort + golden-500 page-based deltas (the gate measurements)**

Run: `uv run python scripts/context_sim.py bands --fixture tests/fixtures/golden_set_archetype_payoff.json --output-dir .audit/subtype_supply/cohort_live`
Run: `uv run python scripts/context_sim.py bands --fixture tests/fixtures/golden_set_run_500.json --output-dir .audit/subtype_supply/g500_live`

(Adjust flag names to the actual CLI if `--output-dir` differs — see `scripts/context_sim.py --help`. If the instrument refuses on a fixture config-hash mismatch, do NOT re-pin in this task; instead compute the same live means with a short driver script over `build_context_sim`/`engine.page` — the instrument's own modules are importable — and note the workaround in the report.)

Compute and record:
- cohort ΔNDCG = live cohort mean − **0.2858**; golden-500 ΔNDCG = live mean − **0.1531**; gem deltas vs 0.9020 / 0.8189.
- Per-commander cohort cliffs: diff live per-commander NDCG against the baseline per-commander values in `.audit/context_sim/cohort_v2/bands.json` (fallback if that file lacks per-commander detail: temporarily set the flag False, rerun bands to regenerate baseline values — they must match the pinned mean bitwise — then flip back).
- Sidecar only: `uv run scripts/bench.py audit --per-commander-ndcg --fixture tests/fixtures/golden_set_archetype_payoff.json` (do not gate on it).

- [ ] **Step 5: Write `.audit/subtype_supply/decision.md`**

One table: every S1–S6 gate, its threshold, the measured value at (producer=2.5, body=1.0), PASS/FAIL. Below it: the golden-100 audit summary, collinearity rows, inspect-gems notes, and the whitelist bar table from Global Constraints for side-by-side reading. No decision yet — Task 5 tunes first.

- [ ] **Step 6: Report** (no commit — leave the working tree dirty for Task 5; state this explicitly in the report)

---

## Task 5: Bounded multiplier tuning (working tree, still nothing committed)

**Grid:** `subtype_supply_producer` ∈ {1.5, 2.5, 3.5} × `subtype_supply_body` ∈ {0.0, 0.5, 1.0} — 9 cells. `body=0.0` is a legitimate operating point (producer-only rule); the whitelist evidence predicts low body values win.

- [ ] **Step 1: Sweep the cohort fixture**

For each cell: edit the two `value` fields in `scoring_weights.json`, run `context_sim bands` on the cohort fixture (as Task 4 Step 4), record (ΔNDCG, gemΔ, cliffs) into a 9-row table in `.audit/subtype_supply/decision.md`. (Weight edits flip the config hash, but bands is a live measurement — no re-pin needed while uncommitted.)

- [ ] **Step 2: Golden-500 + golden-100 on the top 2 cells**

For the two cells with the best cohort ΔNDCG subject to S2 (gemΔ ≥ −0.0242) and S3 (cliffs ≤ 1): run golden-500 bands + `bench.py audit` (golden-100 verdict + gem warning) and record S4/S5 values.

- [ ] **Step 3: Choose the operating point**

The chosen cell is the one passing the most gates; ties break toward fewer cliffs, then better gems. Update `.audit/subtype_supply/decision.md` with the final gate table at the chosen cell and a one-line verdict: **SHIP** (all S1–S6 pass), **PARTIAL** (S2–S6 pass, ΔNDCG ∈ [+0.0567, +0.0697)), or **DECLINE**.

- [ ] **Step 4: Report** the verdict + full table. Still no commit. If PARTIAL: STOP after reporting — the controller escalates to the human before Task 6.

---

## Task 6: Decision execution — SHIP or DECLINE

### SHIP path (all S1–S6 pass at the chosen cell)

- [ ] Set `_ENABLE_SUBTYPE_SUPPLY = True` and the chosen multipliers with final comments (cite the decision table: "Plan 2026-07-07-001 Task 5 sweep: <cell> — cohort ΔNDCG +X.XXXX, gems ΔX.XXXX, cliffs N; beats producer-only whitelist +0.0697.").
- [ ] Re-pin all three fixtures (scoring-config change — `--repin` correctly preserves `cohort_members`):

```bash
uv run scripts/bench.py audit --repin --yes
uv run scripts/bench.py audit --repin --yes --fixture tests/fixtures/golden_set_run_500.json
uv run scripts/bench.py audit --repin --yes --fixture tests/fixtures/golden_set_archetype_payoff.json
```

- [ ] Recompute the cohort reporter noise band (CLAUDE.md instructs this after the baseline moves): bootstrap the `--per-commander-ndcg` in-cohort live values (seed 17) and update the NOISE BAND comment block in CLAUDE.md with the new mean/half-width.
- [ ] Full suite: `uv run pytest tests/ -q` (freshness gates now green against the new pins).
- [ ] Docs: add a dated entry to `docs/RULE_HISTORY.md` (rule ids, gate table, verdict POSITIVE, chosen multipliers); in `docs/solutions/best-practices/deck-context-null-result-2026-07-06.md` update the "Still open" list — subtype-supply rule now TESTED/SHIPPED with a pointer to this plan; update this plan's `**status:**` to `shipped`.
- [ ] `graphify update .`; commit everything as one commit:

```bash
git add -A
git commit -m "feat(rules): ship subtype-supply rule (producer=<P>, body=<B>)

Cohort ΔNDCG +X.XXXX (page-based, vs pinned 0.2858) beats both whitelist
variants (+0.0697 / +0.0523) with gems within noise (ΔX.XXXX ≥ -0.0242) and
<=1 cliff; golden-500 within band. Gates S1-S6 in
docs/plans/2026-07-07-001-feat-subtype-supply-rule-plan.md; decision data
in .audit/subtype_supply/decision.md (gitignored)."
```

### DECLINE path (any of S1/S2/S3/S4/S5/S6 fails at every cell)

- [ ] `git checkout -- src/mtg_synergy_graph/data/scoring_weights.json src/mtg_synergy_graph/complement_rules/subtype_supply.py` (flag back to False, weights entries gone — committed code is already hash-neutral; verify `uv run scripts/bench.py audit --expect-identity` PASS and `uv run pytest tests/ -q` green).
- [ ] Write `docs/solutions/best-practices/subtype-supply-rule-null-result-2026-07-07.md` with the standard YAML frontmatter (`module: complement_rules`, `problem_type: null-result`, `applies_when` including "Planning any subtype-supply / death-payoff supply mechanism" and "Reading the deck-context Whitelist Finding"), the full 9-cell sweep table, which gate(s) killed it and why, and what remains untested. Update the deck-context null-result doc's Whitelist Finding with the outcome, and this plan's `**status:**` to `declined` with a DECISION block.
- [ ] Commit docs + any test-only keepers:

```bash
git add docs/ && git commit -m "docs: subtype-supply rule DECLINED at gates <which> — null-result record

Rule helper + tests + death_payoff module remain as standing infra
(flag-off, hash-neutral, --expect-identity PASS)."
```

---

## Self-review notes (writing-plans checklist)

- **Spec coverage:** Whitelist Finding obligations — beat both variants (S1 vs +0.0697/+0.0523), golden-500 no-regression (S4), don't pay with gems (S2) — all pinned as gates BEFORE any measurement, per kill-test-first discipline. Declarative-row impossibility documented with evidence. Necessary-but-not-sufficient caveat carried from `cohorts.py`.
- **Type consistency:** `payoff_subtypes_from_ports(conn, cmdr_ports) -> list[str]` defined in Task 1, consumed in Task 2; rule_ids `subtype_supply_producer`/`subtype_supply_body` identical across Tasks 2/3/4/5/6.
- **Known judgment calls left to the implementer with guidance:** exact `context_sim.py bands` CLI flags (check `--help`); the fixture's cohort-members JSON key (read the bootstrap script); `PortRow` import location (match `density.py`).
- **Deliberate scope exclusions (YAGNI):** no valid_filter qualifier narrowing (e.g. `!token` handling) beyond what the cohort predicate already does — the whitelist bars were measured without it; no non-death payoff triggers (ETB tribal is `token_producer`/`tribal_density` territory); no declarative migration (grammar can't express it — revisit only if the grammar grows a parameterized-join op).

---

## DECISION (Task 6, 2026-07-07)

**Verdict: PARTIAL, ship approved by human decision (Pareto-dominance
rationale).** S1 did NOT pass (cohort ΔNDCG +0.0650 < the +0.0697
whitelist bar) — this is not a claim that S1 passed. S2–S6 all pass at
the chosen cell.

**Shipped operating point: producer=1.5, body=0.5** (overriding the
plan's own mechanical tie-break of fewer-cliffs-then-better-gems, which
would have selected the sibling cell (1.5, 1.0)).

**Chosen-cell numbers:** cohort ΔNDCG +0.0650, cohort gemΔ −0.0232
(within the −0.0242 band), 1 shallow cliff (Jason Bright, Glowing
Prophet −0.0533); golden-500 ΔNDCG +0.0014 / gemΔ −0.0005; golden-100
audit positive (Δ +30.5059), no gem stderr warning.

**Rationale:** at any side-effect budget the team is willing to accept
(≤1 cliff), (1.5, 0.5) Pareto-dominates both hardcoded-whitelist
comparators (producer-only 0.25 +0.0531 / full 0.50 +0.0523) — higher
ΔNDCG, comparable-or-better gems, no worse cliffs. It also dominates the
plan's mechanical tie-break choice (1.5, 1.0) on cliff depth: (1.5,
1.0)'s single cliff is a −0.1630 hole (Lazav, Wearer of Faces) vs (1.5,
0.5)'s shallow −0.0533, at a cost of only 0.0018 less ΔNDCG and 0.0060
worse gemΔ. The human weighed bounded worst-case per-commander damage
over that small gem-rate edge.

**Cliffs-track-producer-weight finding:** the 3×3 sweep showed cliff
count tracks the `producer` multiplier almost independent of `body`
(0-1 at producer=1.5, 6 at producer=2.5, 9 at producer=3.5); do not
raise `subtype_supply_producer` without re-running the sweep.

Full gate table, sweep data, and provenance: `.audit/subtype_supply/decision.md`
(gitignored). Permanent record: `docs/RULE_HISTORY.md` 2026-07-07 entry.
All three fixtures (`golden_set_run.json`, `golden_set_run_500.json`,
`golden_set_archetype_payoff.json`) re-pinned at config_hash
`5adeea6eea364655c75eb6fca7859bfda092698c67ec2eee7956ce096c928ea1`. The
cohort reporter noise band (`--per-commander-ndcg`, seed 17) moved to
mean 0.1850, half-width 0.0579 (CLAUDE.md updated).
