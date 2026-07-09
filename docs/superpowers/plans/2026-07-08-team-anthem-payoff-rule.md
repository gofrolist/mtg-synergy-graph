# Team-Anthem Payoff Rule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `team_anthem_payoff` complement rule that keys on a commander's own team-scoped `static.Continuous` anthem and pulls creature-token producers as payoffs, serving the ~25 dead-zone team-anthem commanders (Avacyn, Iroas, …) that no current rule touches.

**Architecture:** A flag-gated Python-helper rule in `complement_rules/statics.py`, invoked from `find_all_complements` in `core.py` like the other helper rules. Default OFF and hash-neutral (the `death_outlet_feeder` pattern: flag NOT in `ScoringConfigInputs`) until it clears pre-registered coverage + no-regression gates. A `team_anthem` cohort predicate in `bench/cohorts.py` feeds the coverage instrument's `gate` subcommand for measurement.

**Tech Stack:** Python 3, SQLite (`data/synergy.db`), pytest, `uv run`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-08-team-anthem-payoff-rule-design.md`.
- Working branch: `feat/team-anthem-payoff-rule` (already created; spec committed).
- **Zero scoring-path change while the flag is off.** `_ENABLE_TEAM_ANTHEM_PAYOFF = False` default; the flag is NOT added to `ScoringConfigInputs` in this plan (it is added ONLY in Task 6 if the gates pass). This keeps `compute_config_hash()` unchanged so no re-pin is needed to develop/measure.
- Never pass a literal project-relative DB path to code reaching `open_db()`/`sqlite3.connect()`; tests use `tmp_path` or the shared conn fixture. (CLAUDE.md Conventions.)
- SQL fragment interpolation guarded by frozensets + `ValueError`, never `assert`. (CLAUDE.md.) This plan uses only static SQL + parameter binding, so no new guarded interpolation is introduced.
- `rule_id` lives in EXACTLY ONE path: `team_anthem_payoff` is a Python-helper rule, so it must NOT be added to `DECLARATIVE_RULE_IDS`.
- Run the full suite with `uv run pytest tests/` (~1230 tests, ~1-2s). Run single tests with `uv run pytest <path>::<name> -v`.
- Commit after each task with the standard trailer:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3
  ```

---

## File Structure

- `src/mtg_synergy_graph/complement_rules/statics.py` — MODIFY. Add the flag, the commander-gate helper `_commander_team_anthem_statics`, and the emitter `_find_team_anthem_payoffs`. (Adjacent to the existing `_find_anthem_payoffs`, reusing `_ANTHEM_BUFF_KEYS`, `_TOKENSCRIPT_RE`, `_CREATURE_TOKEN_PT_RE`.)
- `src/mtg_synergy_graph/complement_rules/core.py` — MODIFY. Import + invoke the emitter in `find_all_complements`.
- `src/mtg_synergy_graph/complement_rules/registry.py` — MODIFY. Add a flag-aware `RuleGate("team_anthem_payoff", …)` so coverage-attribution tools (`gap_report`, `rule_quality_gate`) see the truth.
- `src/mtg_synergy_graph/data/scoring_weights.json` — MODIFY (Task 6 only, on ship). Seed the `team_anthem_payoff` quality multiplier at `1.5` in the top-level `rule_quality_multiplier` map. NOT edited earlier because it flips `compute_config_hash`.
- `src/mtg_synergy_graph/bench/cohorts.py` — MODIFY. Add the `team_anthem(conn)` predicate.
- `src/mtg_synergy_graph/bench/coverage_report.py` — MODIFY. Register `"team_anthem"` in `_COHORT_DISPATCH`.
- `tests/complement_rules/test_team_anthem_payoff.py` — CREATE. Gate, candidate-tier, dedup, flag-off tests.
- `tests/bench/test_team_anthem_cohort.py` — CREATE. Cohort predicate + dispatch tests.
- `src/mtg_synergy_graph/universal_scorer.py` — MODIFY (Task 6 only, conditional on gate pass). Register the flag in `ScoringConfigInputs`.

---

## Task 1: Commander-side gate — `_commander_team_anthem_statics`

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/statics.py` (after `_commander_makes_creature_tokens`, ~line 420)
- Test: `tests/complement_rules/test_team_anthem_payoff.py` (create)

**Interfaces:**
- Consumes: `PortRow` (from `.core`), `_ANTHEM_BUFF_KEYS` (already in statics.py).
- Produces: `_commander_team_anthem_statics(cmdr_ports: list[PortRow]) -> list[PortRow]` — returns the subset of commander ports that are qualifying team-anthem statics.

- [ ] **Step 1: Write the failing test**

Create `tests/complement_rules/test_team_anthem_payoff.py`:

```python
from mtg_synergy_graph.complement_rules.statics import _commander_team_anthem_statics


def _static(affected_scope, raw_line):
    return {
        "port_type": "static",
        "event_class": "Continuous",
        "affected_scope": affected_scope,
        "raw_line": raw_line,
    }


def test_youctrl_permanent_keyword_anthem_qualifies():
    # Avacyn shape: grants Indestructible to your other permanents.
    ports = [_static("Permanent.Other+YouCtrl", "{'AddKeyword': 'Indestructible'}")]
    assert _commander_team_anthem_statics(ports) == ports


def test_youctrl_creature_pump_anthem_qualifies():
    # Iroas shape: Menace to your creatures.
    ports = [_static("Creature.YouCtrl", "{'AddKeyword': 'Menace'}")]
    assert len(_commander_team_anthem_statics(ports)) == 1


def test_symmetric_anthem_no_youctrl_rejected():
    # Ascendant Evincar shape: Creature.Black+Other, no YouCtrl (symmetric).
    ports = [_static("Creature.Black+Other", "{'AddPower': '1', 'AddToughness': '1'}")]
    assert _commander_team_anthem_statics(ports) == []


def test_self_only_static_rejected():
    # Voltron sub-shape, out of scope.
    ports = [_static("Card.Self", "{'AddKeyword': 'Double Strike'}")]
    assert _commander_team_anthem_statics(ports) == []


def test_subtype_anthem_rejected():
    # Goblin lord — lord's territory, base type is a subtype not Creature/Permanent.
    ports = [_static("Goblin.YouCtrl", "{'AddPower': '1', 'AddToughness': '1'}")]
    assert _commander_team_anthem_statics(ports) == []


def test_drawback_static_rejected():
    # Negative pump is not a payoff.
    ports = [_static("Creature.YouCtrl", "{'AddPower': '-1', 'AddToughness': '-1'}")]
    assert _commander_team_anthem_statics(ports) == []


def test_non_static_port_ignored():
    trigger = {"port_type": "trigger", "event_class": "Attacks", "affected_scope": "", "raw_line": ""}
    assert _commander_team_anthem_statics([trigger]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/complement_rules/test_team_anthem_payoff.py -v`
Expected: FAIL with `ImportError: cannot import name '_commander_team_anthem_statics'`.

- [ ] **Step 3: Write minimal implementation**

In `src/mtg_synergy_graph/complement_rules/statics.py`, add after `_commander_makes_creature_tokens` (the `_ANTHEM_BUFF_KEYS`, `re` import, and `_NEGATIVE_BUFF_RE` are needed — the negative regex may already be inline in `_find_anthem_payoffs`; define it as a module constant if not present):

```python
_NEGATIVE_BUFF_RE = re.compile(r"'Add(?:Power|Toughness)':\s*'-")

#: Affected-scope base types that make a static a *team* anthem (vs a
#: creature-subtype lord, which stays lord's territory, or Card.Self voltron).
_TEAM_ANTHEM_BASES: frozenset[str] = frozenset({"Creature", "Permanent"})


def _commander_team_anthem_statics(cmdr_ports: list[PortRow]) -> list[PortRow]:
    """Commander statics that are your-team anthems (Unit 1 gate).

    A port qualifies iff it is a ``static.Continuous`` whose ``affected_scope``
    names a ``Creature``/``Permanent`` base with a ``YouCtrl`` controller scope,
    and whose raw_line grants a positive ``AddPower``/``AddToughness`` or an
    ``AddKeyword``. Symmetric anthems (no ``YouCtrl``), ``Card.Self`` voltron
    statics, creature-*subtype* lords, and negative drawback statics are all
    rejected. This is the mirror boundary of ``_find_anthem_payoffs``.
    """
    out: list[PortRow] = []
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "static":
            continue
        if (p.get("event_class") or "").strip() != "Continuous":
            continue
        raw = str(p.get("raw_line") or "")
        if not any(k in raw for k in _ANTHEM_BUFF_KEYS):
            continue
        if _NEGATIVE_BUFF_RE.search(raw):
            continue
        scope = p.get("affected_scope") or ""
        for alt in scope.split(","):
            alt = alt.strip()
            base = alt.split(".")[0].split("+")[0].strip()
            if base not in _TEAM_ANTHEM_BASES:
                continue
            if "YouCtrl" not in alt:
                continue
            out.append(p)
            break
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/complement_rules/test_team_anthem_payoff.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/complement_rules/test_team_anthem_payoff.py src/mtg_synergy_graph/complement_rules/statics.py
git commit -m "feat(rules): team_anthem commander-side gate (Unit 1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3"
```

---

## Task 2: Candidate emitter — `_find_team_anthem_payoffs`

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/statics.py` (after Task 1's helper)
- Test: `tests/complement_rules/test_team_anthem_payoff.py` (append)

**Interfaces:**
- Consumes: `_commander_team_anthem_statics` (Task 1); `_TOKENSCRIPT_RE`, `_CREATURE_TOKEN_PT_RE` (already in statics.py); `PortComplement` (from `.core`).
- Produces: the module flag `statics._ENABLE_TEAM_ANTHEM_PAYOFF: bool = False` (defined here, hash-neutral); `_find_team_anthem_payoffs(conn, cmdr_ports: list[PortRow], cmdr_set: set[str]) -> list[PortComplement]` — flag-guarded (returns `[]` when off), emits `rule_id="team_anthem_payoff"`, `cmdr_event="team_anthem"`, `cand_event ∈ {"token_doubler", "token_producer"}`.

**Detection reference (verified against `data/synergy.db`):**
- Creature-token producer: an `effect.Token` port whose `raw_line` TokenScript matches a P/T (`_\d+_\d+`) — this separates creatures from Treasure/Clue/Food (which have no P/T). Same test `_commander_makes_creature_tokens` already applies to commander ports.
- Token doubler: `port_type='replacement' AND event_class='CreateToken'` (Doubling Season, Parallel Lives, Anointed Procession).

- [ ] **Step 1: Write the failing test**

Append to `tests/complement_rules/test_team_anthem_payoff.py`:

```python
import sqlite3

import pytest

from mtg_synergy_graph.complement_rules import statics as statics_mod
from mtg_synergy_graph.complement_rules.statics import _find_team_anthem_payoffs


@pytest.fixture()
def anthem_conn(monkeypatch):
    # The rule is flag-gated default-OFF (added in this task's implementation
    # step). Every emitter test below exercises the firing path, so enable the
    # flag here; the flag-off test overrides it back to False explicitly.
    monkeypatch.setattr(statics_mod, "_ENABLE_TEAM_ANTHEM_PAYOFF", True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY,
            card_name TEXT,
            port_type TEXT,
            event_class TEXT,
            affected_scope TEXT,
            raw_line TEXT
        );
        """
    )
    # A creature-token producer (P/T in TokenScript).
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Grave Titan', 'effect', 'Token', ?)",
        ("{'TokenScript': 'b_2_2_zombie'}",),
    )
    # A non-creature (Treasure) producer — no P/T in TokenScript.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Smothering Tithe', 'effect', 'Token', ?)",
        ("{'TokenScript': 'c_a_treasure'}",),
    )
    # A token doubler (replacement.CreateToken).
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Doubling Season', 'replacement', 'CreateToken', '{}')",
    )
    conn.commit()
    return conn


_AVACYN_STATIC = {
    "port_type": "static",
    "event_class": "Continuous",
    "affected_scope": "Permanent.Other+YouCtrl",
    "raw_line": "{'AddKeyword': 'Indestructible'}",
}


def test_emitter_creature_producer_fires(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    by_cand = {c.candidate: c for c in comps}
    assert "Grave Titan" in by_cand
    assert by_cand["Grave Titan"].rule_id == "team_anthem_payoff"
    assert by_cand["Grave Titan"].cand_event == "token_producer"


def test_emitter_treasure_maker_excluded(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    assert "Smothering Tithe" not in {c.candidate for c in comps}


def test_emitter_doubler_tier(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    by_cand = {c.candidate: c for c in comps}
    assert by_cand["Doubling Season"].cand_event == "token_doubler"


def test_emitter_no_qualifying_static_returns_empty(anthem_conn):
    self_only = dict(_AVACYN_STATIC, affected_scope="Card.Self")
    assert _find_team_anthem_payoffs(anthem_conn, [self_only], set()) == []


def test_emitter_dedup_single_complement_per_candidate(anthem_conn):
    # A card that is BOTH a doubler and a creature producer resolves to ONE
    # complement, at the strong (doubler) tier.
    anthem_conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Ojer Taq', 'effect', 'Token', ?)",
        ("{'TokenScript': 'w_1_1_human'}",),
    )
    anthem_conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Ojer Taq', 'replacement', 'CreateToken', '{}')",
    )
    anthem_conn.commit()
    comps = [c for c in _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set()) if c.candidate == "Ojer Taq"]
    assert len(comps) == 1
    assert comps[0].cand_event == "token_doubler"


def test_emitter_excludes_commander_itself(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], {"Grave Titan"})
    assert "Grave Titan" not in {c.candidate for c in comps}


def test_emitter_flag_off_returns_empty(anthem_conn, monkeypatch):
    # anthem_conn enabled the flag; override it off — the guard must short-circuit.
    monkeypatch.setattr(statics_mod, "_ENABLE_TEAM_ANTHEM_PAYOFF", False)
    assert _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/complement_rules/test_team_anthem_payoff.py -k emitter -v`
Expected: FAIL with `ImportError: cannot import name '_find_team_anthem_payoffs'` (or `AttributeError` on the missing `_ENABLE_TEAM_ANTHEM_PAYOFF` flag).

- [ ] **Step 3: Write minimal implementation**

3a. First, add the flag near the top of `statics.py` (after imports, module-level). The rule is flag-gated from birth, default OFF and hash-neutral (NOT in `ScoringConfigInputs` — Task 6 registers it there only on ship). Mirrors `death_outlet._ENABLE_DEATH_OUTLET_FEEDER`:

```python
#: Coverage-oriented rule (spec 2026-07-08-team-anthem-payoff-rule). Default
#: OFF and hash-neutral until it clears the pre-registered coverage +
#: no-regression gates.
_ENABLE_TEAM_ANTHEM_PAYOFF: bool = False
```

3b. Then add the emitter, after Task 1's helper — its FIRST body line is the flag guard:

```python
def _find_team_anthem_payoffs(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Token-producer payoffs for passive team-anthem commanders (Unit 2).

    Fires when the commander has a qualifying team-anthem static (Unit 1).
    Candidates are creature-token producers, tiered ``token_doubler`` (strong,
    replacement.CreateToken) > ``token_producer`` (effect.Token with a P/T
    TokenScript). Dedup per candidate, strong tier wins. Reverse direction of
    ``_find_anthem_payoffs`` (commander IS the anthem; candidates make bodies),
    so no (cmdr_port, cand_port) pair is double-scored.
    """
    if not _ENABLE_TEAM_ANTHEM_PAYOFF:
        return []
    if not _commander_team_anthem_statics(cmdr_ports):
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Strong tier first so dedup keeps the doubler credit.
    for (name,) in conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'replacement' AND event_class = 'CreateToken'"
    ).fetchall():
        if name in cmdr_set or name in seen:
            continue
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="team_anthem_payoff",
                direction="synergy",
                candidate=name,
                cmdr_event="team_anthem",
                cand_event="token_doubler",
            )
        )

    for row in conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Token'"
    ).fetchall():
        name = row["card_name"]
        if name in cmdr_set or name in seen:
            continue
        m = _TOKENSCRIPT_RE.search(str(row["raw_line"] or ""))
        if not (m and _CREATURE_TOKEN_PT_RE.search(m.group(1))):
            continue
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="team_anthem_payoff",
                direction="synergy",
                candidate=name,
                cmdr_event="team_anthem",
                cand_event="token_producer",
            )
        )

    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/complement_rules/test_team_anthem_payoff.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/complement_rules/test_team_anthem_payoff.py src/mtg_synergy_graph/complement_rules/statics.py
git commit -m "feat(rules): team_anthem token-producer/doubler emitter (Unit 2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3"
```

---

## Task 3: Wiring + flag-aware RuleGate (Unit 3)

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/core.py` (import + invoke)
- Modify: `src/mtg_synergy_graph/complement_rules/registry.py` (flag-aware RuleGate)
- Test: `tests/complement_rules/test_team_anthem_payoff.py` (append)

(The flag `_ENABLE_TEAM_ANTHEM_PAYOFF` and its guard were added in Task 2. The `scoring_weights.json` multiplier is deliberately NOT edited here — it would flip the config hash; it is added in Task 6.)

**Interfaces:**
- Consumes: `_find_team_anthem_payoffs`, `statics._ENABLE_TEAM_ANTHEM_PAYOFF`, `statics._commander_team_anthem_statics` (all Task 1/2).
- Produces: the emitter invoked in `find_all_complements`; a `RuleGate("team_anthem_payoff", _team_anthem_payoff_gate)` in `RULE_GATES`.

- [ ] **Step 1: Write the failing test**

Append to `tests/complement_rules/test_team_anthem_payoff.py` (`statics_mod` is already imported at the top of the file from Task 2):

```python
from mtg_synergy_graph.complement_rules.registry import attributable_rules_for_port


def test_rule_gate_flag_aware():
    port = {
        "port_type": "static",
        "event_class": "Continuous",
        "affected_scope": "Creature.YouCtrl",
        "raw_line": "{'AddKeyword': 'Menace'}",
    }
    # Flag off: gate reports NO coverage so gap_report/quality see the truth.
    statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = False
    assert "team_anthem_payoff" not in attributable_rules_for_port(port)
    statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = True
    try:
        assert "team_anthem_payoff" in attributable_rules_for_port(port)
    finally:
        statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = False
```

Note: `attributable_rules_for_port(port) -> frozenset[str]` is the existing registry export (registry.py ~line 921) that returns the rule_ids whose gate matches a port.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/complement_rules/test_team_anthem_payoff.py -k rule_gate -v`
Expected: FAIL — `team_anthem_payoff` not in the gate set.

- [ ] **Step 3: Write minimal implementation**

3a. In `core.py`, add the import next to the other statics imports (find the block importing from `.statics`; if the emitter is imported via `from .statics import (...)`, add `_find_team_anthem_payoffs` there — otherwise add a dedicated import line), then add the invocation in `find_all_complements` next to `_find_anthem_payoffs`/`_find_subtype_supply_complements` (around line 1358):

```python
        out.extend(_find_team_anthem_payoffs(conn, cmdr_ports, cmdr_set))
```

3b. In `registry.py`, add a flag-aware gate mirroring `_death_outlet_feeder_gate` (line ~666). Place the predicate near it and add the `RuleGate` to `_CARD_ATTR_GATES` (the tuple at line ~724):

```python
def _team_anthem_payoff_gate(port: PortRow) -> bool:
    """Single-port shape for team_anthem_payoff coverage attribution.

    Flag-aware: reads ``statics._ENABLE_TEAM_ANTHEM_PAYOFF`` at CALL time so
    gap_report / rule_quality_gate see NO coverage while the rule is off
    (mirrors ``_death_outlet_feeder_gate``). Composes the Unit-1 gate on a
    one-element port list.
    """
    from . import statics
    if not statics._ENABLE_TEAM_ANTHEM_PAYOFF:
        return False
    return bool(statics._commander_team_anthem_statics([port]))
```

Add to `_CARD_ATTR_GATES`:

```python
    RuleGate("team_anthem_payoff", _team_anthem_payoff_gate),
```

3c. **No `scoring_weights.json` edit in this task.** Editing that file flips `compute_config_hash` (CLAUDE.md), which would break the hash-neutrality this task asserts in Step 4. Rules absent from `rule_quality_multiplier` default to a 1.0 multiplier, which is correct for the flag-off state. The `1.5` weight is added in Task 6 (ship), where a re-pin happens anyway; Task 5 measurement applies `1.5` in-process so the on-disk hash stays neutral during measurement.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/complement_rules/test_team_anthem_payoff.py -v`
Expected: PASS (all tests).

Then verify hash-neutrality (the flag is off and NOT in ScoringConfigInputs, so the config hash must be unchanged from `main`):

Run: `uv run python -c "from mtg_synergy_graph.bench.tensor import compute_config_hash; print(compute_config_hash())"`
Expected: prints `c770b664e626...` (same 12-char prefix as the pinned baseline/`main`). If it differs, the flag leaked into `ScoringConfigInputs` — remove it (Task 6 adds it, not this task).

Then run the full suite to confirm no scoring-path regression:

Run: `uv run pytest tests/ -q`
Expected: all pass (existing golden-set no-regression assertions included).

- [ ] **Step 5: Commit**

```bash
git add tests/complement_rules/test_team_anthem_payoff.py src/mtg_synergy_graph/complement_rules/core.py src/mtg_synergy_graph/complement_rules/registry.py
git commit -m "feat(rules): wire team_anthem_payoff + flag-aware RuleGate (Unit 3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3"
```

---

## Task 4: `team_anthem` cohort predicate + dispatch (Unit 4)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cohorts.py` (add predicate)
- Modify: `src/mtg_synergy_graph/bench/coverage_report.py` (register in `_COHORT_DISPATCH`, line ~39)
- Test: `tests/bench/test_team_anthem_cohort.py` (create)

**Interfaces:**
- Consumes: `LEGAL_LEGENDARY_CREATURE_WHERE` (cohorts.py).
- Produces: `team_anthem(conn: sqlite3.Connection) -> set[str]`; `_COHORT_DISPATCH["team_anthem"]`.

- [ ] **Step 1: Write the failing test**

Create `tests/bench/test_team_anthem_cohort.py`:

```python
from mtg_synergy_graph.bench.cohorts import team_anthem
from mtg_synergy_graph.bench.coverage_report import _COHORT_DISPATCH
from mtg_synergy_graph.db import open_db


def test_team_anthem_includes_known_members():
    conn = open_db("data/synergy.db", create=False)
    members = team_anthem(conn)
    assert "Avacyn, Angel of Hope" in members
    assert "Iroas, God of Victory" in members


def test_team_anthem_excludes_symmetric_anthem():
    conn = open_db("data/synergy.db", create=False)
    # Ascendant Evincar is symmetric (Creature.Black+Other, no YouCtrl).
    assert "Ascendant Evincar" not in team_anthem(conn)


def test_team_anthem_registered_in_dispatch():
    assert _COHORT_DISPATCH.get("team_anthem") is team_anthem
```

Note: these read the real `data/synergy.db`. If the suite lacks that DB in CI, guard with a skip mirroring other `tests/bench` DB-dependent tests — check an existing one (`grep -rl "open_db(\"data/synergy.db\"" tests/bench` or the `pytest.importorskip`/skip pattern used there) and copy it.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/bench/test_team_anthem_cohort.py -v`
Expected: FAIL — `cannot import name 'team_anthem'`.

- [ ] **Step 3: Write minimal implementation**

3a. In `cohorts.py`, add (after `toughness_payoff`, ~line 191):

```python
def team_anthem(conn: sqlite3.Connection) -> set[str]:
    """Legal legendary-creature commanders whose payoff is a passive team anthem.

    Qualifies when the commander has a ``static.Continuous`` port whose
    ``affected_scope`` names a ``Creature``/``Permanent`` base with a
    ``YouCtrl`` controller scope and a positive ``AddPower``/``AddToughness``
    or ``AddKeyword`` (creature-*subtype* bases stay lord's territory;
    ``Card.Self`` and symmetric/no-``YouCtrl`` scopes are excluded). This is
    the target cohort of the ``team_anthem_payoff`` rule (spec 2026-07-08);
    deliberately NOT part of any shared cohort union. Mirrors
    ``complement_rules.statics._commander_team_anthem_statics`` — that helper
    is the single source of truth for the qualifying-static predicate.
    """
    from mtg_synergy_graph.complement_rules.statics import _commander_team_anthem_statics

    rows = conn.execute(
        "SELECT p.card_name, p.port_type, p.event_class, p.affected_scope, p.raw_line "
        "FROM card_ports p JOIN cards c ON c.name = p.card_name "
        "WHERE p.port_type = 'static' AND p.event_class = 'Continuous' "
        "AND p.affected_scope IS NOT NULL AND p.affected_scope != '' "
        "AND " + LEGAL_LEGENDARY_CREATURE_WHERE
    )
    out: set[str] = set()
    for row in rows:
        if _commander_team_anthem_statics([dict(row)]):
            out.add(row["card_name"])
    return out
```

(Reusing `_commander_team_anthem_statics` guarantees the cohort predicate and the rule gate cannot drift — the CLAUDE.md single-source discipline.)

3b. In `coverage_report.py`, extend the dispatch (line ~39). Update the import at the top (currently `from mtg_synergy_graph.bench.cohorts import LEGAL_LEGENDARY_CREATURE_WHERE, toughness_payoff`) to also import `team_anthem`, then:

```python
_COHORT_DISPATCH = {"toughness_payoff": toughness_payoff, "team_anthem": team_anthem}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/bench/test_team_anthem_cohort.py -v`
Expected: PASS (3 passed).

Sanity-check the cohort size matches the spec's measured ~155:

Run: `uv run python -c "from mtg_synergy_graph.db import open_db; from mtg_synergy_graph.bench.cohorts import team_anthem; print(len(team_anthem(open_db('data/synergy.db', create=False))))"`
Expected: `155` (±a few if the DB was refreshed).

- [ ] **Step 5: Commit**

```bash
git add tests/bench/test_team_anthem_cohort.py src/mtg_synergy_graph/bench/cohorts.py src/mtg_synergy_graph/bench/coverage_report.py
git commit -m "feat(bench): team_anthem cohort predicate + coverage dispatch (Unit 4)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3"
```

---

## Task 5: Measurement — run the pre-registered gates

This task runs diagnostics and records numbers. It makes NO scoring-path change (the flag is still off; we flip it on a *temporary in-process* basis for measurement via monkeypatch/env, never committed here). The decision to ship is Task 6.

**Files:**
- Create: `.audit/team_anthem/` (gitignored working dir for measurement outputs — no commit)

- [ ] **Step 1: Measure the coverage gate (primary).**

The `gate` subcommand requires the flag ON at scoring time and a baseline at the SAME config_hash. Because turning the flag on flips the config hash only AFTER Task 6 registers it in `ScoringConfigInputs`, for measurement we run with the flag on but the hash still neutral — so the pinned baseline (`c770b664...`) stays valid. Enable the flag for the measurement process only:

Run:
```bash
cd /Users/evgenii.vasilenko/gofrolist/mtg-synergy-graph
uv run python - <<'PY'
from mtg_synergy_graph.complement_rules import statics
statics._ENABLE_TEAM_ANTHEM_PAYOFF = True
# Apply the intended shipped multiplier in-process (keeps the on-disk config
# hash neutral so the pinned baseline stays valid). Verify the module attr name
# with: grep -n "rule_quality_multiplier\|_RULE_QUALITY_MULTIPLIER" src/mtg_synergy_graph/universal_scorer.py
from mtg_synergy_graph import universal_scorer
universal_scorer._RULE_QUALITY_MULTIPLIER["team_anthem_payoff"] = 1.5
from mtg_synergy_graph.bench import coverage_report as cr
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.bench.tensor import compute_config_hash

engine, conn = cr._open_engine_and_conn("data/synergy.db")
baseline_hash, baseline = cr.read_baseline(cr._DEFAULT_BASELINE)
cohort = sorted(cr._resolve_cohort("team_anthem", conn))
res = cr.run_gate(engine, conn, cohort, baseline=baseline, control_size=200, seed=17)

# Headroom subset = cohort members dead at baseline.
dead = [n for n in cohort if baseline.get(n) and baseline[n].earned_top30 == 0]
subset_deltas = [res.cohort_deltas[n] for n in dead if n in res.cohort_deltas]
mean_subset = sum(subset_deltas) / len(subset_deltas) if subset_deltas else 0.0
cohort_regressions = {n: d for n, d in res.cohort_deltas.items() if d < 0}
control_regressions = {n: d for n, d in res.control_deltas.items() if d < 0}

print(f"config_hash (must be neutral): {compute_config_hash()[:12]}")
print(f"cohort size: {len(cohort)}  dead-at-baseline (headroom): {len(dead)}")
print(f"PRIMARY headroom-subset mean Delta earned_top30: {mean_subset:+.3f}  (pass >= +5)")
print(f"cohort regressions (must be empty): {cohort_regressions}")
print(f"control mean Delta: {res.control_delta_mean:+.3f}  (pass >= 0)")
print(f"control regressions (must be empty): {control_regressions}")
engine.close(); conn.close()
PY
```
Record: headroom-subset mean Δ, cohort regressions, control mean Δ, control regressions.
Pass condition: `mean_subset >= +5` AND no cohort regressions AND `control mean >= 0` AND no control regressions.

- [ ] **Step 2: Measure golden-500 no-regression (Guard A).**

The `bench.py audit` reads the pinned tensor. Since the flag on is hash-neutral, the audit compares live (flag-on) scores vs the pinned baseline at the same hash. Run with the flag forced on via a one-off env shim:

Run:
```bash
cd /Users/evgenii.vasilenko/gofrolist/mtg-synergy-graph
uv run python - <<'PY'
from mtg_synergy_graph.complement_rules import statics
statics._ENABLE_TEAM_ANTHEM_PAYOFF = True
from mtg_synergy_graph import universal_scorer
universal_scorer._RULE_QUALITY_MULTIPLIER["team_anthem_payoff"] = 1.5
import sys
sys.argv = ["bench.py", "audit", "--fixture", "tests/fixtures/golden_set_run_500.json", "--format", "json"]
from mtg_synergy_graph.bench import main  # entrypoint confirmed: scripts/bench.py does `from mtg_synergy_graph.bench import main`
main()
PY
```
(The goal is: run the 500-fixture audit with the flag + multiplier on and capture the aggregate NDCG@30 + verdict + `hidden_gem_hit_rate`.)
Record: aggregate NDCG@30 delta vs baseline, verdict, `hidden_gem_hit_rate` (Guard D). Pass: NDCG drop within the fixture noise half-width; hidden_gem_hit_rate not materially down.

- [ ] **Step 3: Measure collinearity (Guard B).**

Run: `uv run scripts/bench.py audit --collinearity` (with the flag on via the same shim pattern if the CLI doesn't expose a flag). Confirm `team_anthem_payoff` is not near-parallel (VIF / Pearson) to `scaling` or `anthem_payoff`.
Record the VIF/correlation row for `team_anthem_payoff`. Pass: not flagged as redundant.

- [ ] **Step 4: Measure the rule quality gate (Guard C).**

Run: `uv run python scripts/rule_quality_gate.py --rule team_anthem_payoff` (flag on via shim if needed).
Record: PASS / FAIL and the vacuum-fill / flat-noise metrics.

- [ ] **Step 5: Record all numbers** in the plan's scratch area or a comment on the branch PR. No commit (diagnostics only).

**Decision rule:** ALL of {primary, Guard A, B, C, D} pass → proceed to Task 6 (ship). ANY fails → skip Task 6, go to Task 7 (null-result).

---

## Task 6: Ship (ONLY if Task 5 fully passed)

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/statics.py` (`_ENABLE_TEAM_ANTHEM_PAYOFF = True`)
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (register flag in `ScoringConfigInputs`)
- Modify: `docs/RULE_HISTORY.md` (dated ship entry)
- Re-pin: all three fixtures + the coverage baseline.

- [ ] **Step 1: Flip the flag.** In `statics.py`: `_ENABLE_TEAM_ANTHEM_PAYOFF = True`.

- [ ] **Step 2: Register in `ScoringConfigInputs`** so the on-state is hash-visible (mirrors subtype-supply's review lesson — grep `enable_subtype_supply` in `universal_scorer.py` for the exact three edit sites: the NamedTuple field + docstring `#:` comment near line 324, and the constructor kwarg near line 384). Add:
  - Field: `enable_team_anthem_payoff: bool` in the NamedTuple.
  - Constructor: `enable_team_anthem_payoff=statics._ENABLE_TEAM_ANTHEM_PAYOFF,` (import `statics` the same way `_subtype_supply` is imported there).

- [ ] **Step 3: Confirm the hash flipped.**

Run: `uv run python -c "from mtg_synergy_graph.bench.tensor import compute_config_hash; print(compute_config_hash()[:12])"`
Expected: a NEW hash (not `c770b664`).

- [ ] **Step 4: Re-pin the fixtures** (config hash changed → all pins stale):

```bash
uv run scripts/bench.py audit --repin --yes --fixture tests/fixtures/golden_set_run.json
uv run scripts/bench.py audit --repin --yes --fixture tests/fixtures/golden_set_run_500.json
uv run python scripts/bootstrap_archetype_payoff_fixture.py
uv run scripts/coverage_report.py census   # re-pin the coverage baseline at the new hash
```

- [ ] **Step 5: Full suite + audit green.**

Run: `uv run pytest tests/ -q` → all pass.
Run: `uv run scripts/bench.py audit` → verdict not a regression.

- [ ] **Step 6: RULE_HISTORY entry** — add a dated section to `docs/RULE_HISTORY.md` summarizing the rule, the measured headroom-subset lift, the cohort size, and the guard results (follow the format of the 2026-07-07 subtype-supply entry).

- [ ] **Step 7: Commit + open PR.**

```bash
git add -A
git commit -m "feat(rules): ship team_anthem_payoff — coverage gate passed

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3"
```

---

## Task 7: Null-result (ONLY if Task 5 failed a gate)

**Files:**
- Create: `docs/solutions/best-practices/team-anthem-payoff-null-result-2026-07-08.md`
- Keep: `_ENABLE_TEAM_ANTHEM_PAYOFF = False` (rule stays in-tree, hash-neutral, like `death_outlet_feeder`).

- [ ] **Step 1: Write the null-result doc** with YAML frontmatter (`module: complement_rules`, `tags: [coverage, anthem]`, `problem_type: null-result`), following the format of `docs/solutions/best-practices/death-outlet-feeder-null-result-2026-07-07.md`. Record which gate failed and the exact numbers from Task 5.

- [ ] **Step 2: Leave the flag off.** Confirm hash-neutral:

Run: `uv run python -c "from mtg_synergy_graph.bench.tensor import compute_config_hash; print(compute_config_hash()[:12])"`
Expected: `c770b664` (unchanged).

- [ ] **Step 3: Commit.**

```bash
git add docs/solutions/best-practices/team-anthem-payoff-null-result-2026-07-08.md
git commit -m "docs: team_anthem_payoff DECLINED at pre-registered gates — null-result

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3"
```

---

## Self-Review notes

- **Spec coverage:** Unit 1 → Task 1; Unit 2 → Task 2; Unit 3 (flag/wiring/RuleGate/weight) → Task 3; Unit 4 (cohort+dispatch) → Task 4; pre-registered gates → Task 5; ship path → Task 6; decline path → Task 7. All spec sections mapped.
- **Hash-neutrality:** enforced as an explicit assertion in Task 3 Step 4 and Task 7 Step 2; the flag enters `ScoringConfigInputs` only in Task 6 (ship).
- **Single-source discipline:** the cohort predicate (Task 4) reuses `_commander_team_anthem_statics` (Task 1) so gate and cohort cannot drift.
- **Type consistency:** `_commander_team_anthem_statics(list[PortRow]) -> list[PortRow]` and `_find_team_anthem_payoffs(conn, list[PortRow], set[str]) -> list[PortComplement]` used identically across Tasks 1-4; `cand_event ∈ {"token_doubler","token_producer"}` and `cmdr_event="team_anthem"` consistent.
- **Resolved during planning** (verified against the tree): registry export is `attributable_rules_for_port(port) -> frozenset[str]`; `scoring_weights.json` top key is `rule_quality_multiplier` with `{"value","comment"}` entries; bench entrypoint is `from mtg_synergy_graph.bench import main`; the config-hash cost of a weights edit is why it moved to Task 6.
- **Still verify by grep at execution** (do not assume): the `universal_scorer` multiplier attribute name (`_RULE_QUALITY_MULTIPLIER` — Task 5 snippets), the exact three `ScoringConfigInputs` edit sites (Task 6 Step 2), and the `tests/bench` DB-skip pattern (Task 4 Step 1).
