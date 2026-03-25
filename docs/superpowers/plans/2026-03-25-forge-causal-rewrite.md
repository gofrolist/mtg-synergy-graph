# Forge Causal Graph Rewrite — Implementation Plan (Plan B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the causal interaction graph on Forge's 135 trigger modes and 245 effect verbs, replacing the old 25-trigger/22-verb system. Forge data (32k cards) becomes the primary source; a new oracle parser handles gap cards.

**Architecture:** A verb→event mapping table translates Forge effect verbs to the trigger modes they produce (e.g., Token → ChangesZone). A new Forge-native indexer builds producer/responder indexes from `forge_abilities`. ForgeFilter matching replaces the old filter precision logic. `parse_card()` does Forge DB lookup first, new regex parser as fallback. IDF + chain scoring carry over.

**Tech Stack:** Python 3, SQLite, pytest. Depends on Plan A (forge_types, forge_import, forge_filter_parser already done).

**Spec:** `docs/superpowers/specs/2026-03-25-forge-native-architecture-design.md` (Sections 3, 4, 6, 7)

---

## File Structure

| File | Responsibility | Status |
|------|---------------|--------|
| `mtg_synergy/causal/verb_event_map.py` | Forge verb → trigger mode mapping table | Create |
| `mtg_synergy/causal/forge_indexer.py` | Index forge_abilities by events produced/consumed | Create |
| `mtg_synergy/causal/forge_graph_builder.py` | Build edges using ForgeFilter matching + IDF | Create |
| `mtg_synergy/causal/__init__.py` | Update CausalContext to use new edges | Modify |
| `mtg_synergy/parse/__init__.py` | New parse_card() with Forge lookup + fallback | Modify |
| `build_graph.py` | Update to use Forge-native graph builder | Modify |
| `tests/test_verb_event_map.py` | Verb→event mapping tests | Create |
| `tests/test_forge_indexer.py` | Forge indexer tests | Create |
| `tests/test_forge_graph_builder.py` | Edge building + filter matching tests | Create |
| `tests/test_forge_causal_integration.py` | End-to-end: import → index → score | Create |

---

## Task 1: Verb→Event Mapping Table

**Files:**
- Create: `mtg_synergy/causal/verb_event_map.py`
- Test: `tests/test_verb_event_map.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_verb_event_map.py`:

```python
"""Tests for Forge verb → trigger event mapping."""
import pytest
from mtg_synergy.causal.verb_event_map import verb_to_events, event_to_verbs


def test_token_produces_changes_zone():
    events = verb_to_events("Token")
    assert any(e["trigger_mode"] == "ChangesZone" for e in events)
    # Token creates a creature entering the battlefield
    czs = [e for e in events if e["trigger_mode"] == "ChangesZone"]
    assert any(e.get("destination") == "Battlefield" for e in czs)


def test_deal_damage_produces_damage_done():
    events = verb_to_events("DealDamage")
    assert any(e["trigger_mode"] == "DamageDone" for e in events)


def test_destroy_produces_changes_zone_to_graveyard():
    events = verb_to_events("Destroy")
    czs = [e for e in events if e["trigger_mode"] == "ChangesZone"]
    assert any(e.get("destination") == "Graveyard" for e in czs)


def test_sacrifice_produces_sacrificed():
    events = verb_to_events("Sacrifice")
    assert any(e["trigger_mode"] == "Sacrificed" for e in events)


def test_draw_produces_drawn():
    events = verb_to_events("Draw")
    assert any(e["trigger_mode"] == "Drawn" for e in events)


def test_gain_life_produces_life_gained():
    events = verb_to_events("GainLife")
    assert any(e["trigger_mode"] == "LifeGained" for e in events)


def test_reverse_lookup():
    """Verbs that produce ChangesZone events."""
    verbs = event_to_verbs("ChangesZone")
    assert "Token" in verbs
    assert "ChangeZone" in verbs
    assert "Destroy" in verbs


def test_unknown_verb():
    events = verb_to_events("SomeUnknownVerb")
    assert events == []


def test_pump_has_no_trigger():
    """Pump doesn't produce a triggerable event."""
    events = verb_to_events("Pump")
    assert events == []
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement verb_event_map.py**

Create `mtg_synergy/causal/verb_event_map.py`:

```python
"""Mapping from Forge effect verbs to the trigger modes they produce.

When a card has an effect verb (e.g., Token), it produces game events
that other cards' triggers respond to (e.g., ChangesZone with
Destination=Battlefield).

This replaces the old verb_resolvers.py StateChange system.
"""

# Each entry: verb → list of {trigger_mode, origin?, destination?, card_type?}
_VERB_EVENT_MAP = {
    # Zone changes
    "Token": [
        {"trigger_mode": "ChangesZone", "destination": "Battlefield"},
    ],
    "ChangeZone": [
        {"trigger_mode": "ChangesZone"},  # origin/dest from the ability itself
    ],
    "ChangeZoneAll": [
        {"trigger_mode": "ChangesZone"},
    ],
    "Destroy": [
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
    ],
    "DestroyAll": [
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
    ],
    "Mill": [
        {"trigger_mode": "ChangesZone", "origin": "Library", "destination": "Graveyard"},
    ],
    "Exile": [
        {"trigger_mode": "ChangesZone", "destination": "Exile"},
    ],
    "ExileAll": [
        {"trigger_mode": "ChangesZone", "destination": "Exile"},
    ],
    "CopyPermanent": [
        {"trigger_mode": "ChangesZone", "destination": "Battlefield"},
    ],

    # Damage
    "DealDamage": [
        {"trigger_mode": "DamageDone"},
    ],
    "DamageAll": [
        {"trigger_mode": "DamageDone"},
    ],

    # Life
    "GainLife": [
        {"trigger_mode": "LifeGained"},
    ],

    # Cards
    "Draw": [
        {"trigger_mode": "Drawn"},
    ],
    "Dig": [
        {"trigger_mode": "Drawn"},
    ],
    "Discard": [
        {"trigger_mode": "Discarded"},
    ],

    # Sacrifice
    "Sacrifice": [
        {"trigger_mode": "Sacrificed"},
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
    ],

    # Tap
    "Tap": [
        {"trigger_mode": "Taps"},
    ],
    "TapAll": [
        {"trigger_mode": "Taps"},
    ],

    # Counters (no standard trigger mode, but some cards trigger on counters)
    "PutCounter": [
        {"trigger_mode": "CounterAdded"},
    ],
    "PutCounterAll": [
        {"trigger_mode": "CounterAdded"},
    ],

    # These verbs don't produce triggerable events
    # Pump, PumpAll, Animate, AnimateAll, GainControl, Counter,
    # Scry, Untap, UntapAll, LoseLife, Mana, ReduceCost, etc.
}

# Build reverse map: trigger_mode → set of verbs
_EVENT_VERB_MAP = {}
for verb, events in _VERB_EVENT_MAP.items():
    for event in events:
        mode = event["trigger_mode"]
        _EVENT_VERB_MAP.setdefault(mode, set()).add(verb)


def verb_to_events(verb: str) -> list[dict]:
    """Get the trigger events produced by a Forge effect verb.

    Returns list of dicts with trigger_mode and optional origin/destination.
    """
    return _VERB_EVENT_MAP.get(verb, [])


def event_to_verbs(trigger_mode: str) -> set[str]:
    """Get which Forge verbs can produce a given trigger mode."""
    return _EVENT_VERB_MAP.get(trigger_mode, set())
```

- [ ] **Step 4: Run tests, full suite, commit**

```bash
python3 -m pytest tests/test_verb_event_map.py -v
python3 -m pytest tests/ -q --tb=short
git add mtg_synergy/causal/verb_event_map.py tests/test_verb_event_map.py
git commit -m "feat(causal): add Forge verb→event mapping table"
```

---

## Task 2: Forge-Native Indexer

**Files:**
- Create: `mtg_synergy/causal/forge_indexer.py`
- Test: `tests/test_forge_indexer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_forge_indexer.py`:

```python
"""Tests for Forge-native causal indexer."""
import sqlite3
import pytest
from mtg_synergy.causal.forge_indexer import ForgeIndex, build_forge_index
from mtg_synergy.parse.forge_import import ensure_forge_schema, parse_forge_card_file, import_card_to_db


KRENKO = """Name:Krenko, Mob Boss
ManaCost:2 R R
Types:Legendary Creature Goblin Warrior
PT:3/3
A:AB$ Token | Cost$ T | TokenScript$ r_1_1_goblin | TokenAmount$ X | References$ X | SpellDescription$ Create X 1/1 red Goblin creature tokens.
SVar:X:Count$Valid Goblin.YouCtrl
DeckHas:Ability$Token
DeckHints:Type$Goblin
Oracle:{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control."""

PURPHOROS = """Name:Purphoros, God of the Forge
ManaCost:3 R
Types:Legendary Enchantment Creature God
PT:6/5
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield | TriggerDescription$ Whenever a creature enters the battlefield under your control, Purphoros deals 2 damage to each opponent.
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 2
Oracle:Whenever a creature enters the battlefield under your control, Purphoros, God of the Forge deals 2 damage to each opponent."""

IMPACT = """Name:Impact Tremors
ManaCost:1 R
Types:Enchantment
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield | TriggerDescription$ Whenever a creature enters the battlefield under your control, Impact Tremors deals 1 damage to each opponent.
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 1
Oracle:Whenever a creature enters the battlefield under your control, Impact Tremors deals 1 damage to each opponent."""

BLOOD_ARTIST = """Name:Blood Artist
ManaCost:1 B
Types:Creature Vampire
PT:0/1
T:Mode$ ChangesZone | ValidCard$ Creature | Origin$ Battlefield | Destination$ Graveyard | Execute$ TrigDrain | TriggerZones$ Battlefield | TriggerDescription$ Whenever Blood Artist or another creature dies, target opponent loses 1 life and you gain 1 life.
SVar:TrigDrain:DB$ LoseLife | Defined$ Player.Opponent | LifeAmount$ 1 | SubAbility$ DBGainLife
SVar:DBGainLife:DB$ GainLife | Defined$ You | LifeAmount$ 1
Oracle:Whenever Blood Artist or another creature dies, target opponent loses 1 life and you gain 1 life."""


def _setup_forge_db(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    for card_text in [KRENKO, PURPHOROS, IMPACT, BLOOD_ARTIST]:
        card = parse_forge_card_file(card_text)
        import_card_to_db(conn, card)
    conn.commit()
    return conn


def test_build_forge_index(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    assert isinstance(idx, ForgeIndex)
    assert idx.total_cards > 0
    conn.close()


def test_producers_for_token(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    # Krenko has Token verb → produces ChangesZone(Destination=Battlefield)
    producers = idx.producers_for("ChangesZone")
    krenko_entries = [p for p in producers if p[0] == "Krenko, Mob Boss"]
    assert len(krenko_entries) >= 1
    conn.close()


def test_responders_for_changes_zone(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    # Purphoros and Impact Tremors respond to ChangesZone(Creature, Battlefield)
    responders = idx.responders_for("ChangesZone")
    names = {r[0] for r in responders}
    assert "Purphoros, God of the Forge" in names
    assert "Impact Tremors" in names
    conn.close()


def test_blood_artist_responds_to_dies(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    # Blood Artist triggers on ChangesZone(Battlefield→Graveyard) = creature dies
    responders = idx.responders_for("ChangesZone")
    ba = [r for r in responders if r[0] == "Blood Artist"]
    assert len(ba) >= 1
    conn.close()


def test_idf_computation(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    idf = idx.compute_event_idf()
    # ChangesZone has multiple responders → lower IDF than rare events
    assert "ChangesZone" in idf["responder"]
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement forge_indexer.py**

Create `mtg_synergy/causal/forge_indexer.py`:

```python
"""Forge-native causal indexer — indexes forge_abilities by events produced/consumed.

Replaces the old indexer.py which used parsed AST abilities + verb_resolvers.
This version reads directly from forge_abilities table and uses the
verb→event mapping to determine what events each card produces.
"""
import math
from collections import defaultdict
from dataclasses import dataclass, field

from mtg_synergy.causal.verb_event_map import verb_to_events
from mtg_synergy.parse.forge_filter_parser import parse_forge_filter


@dataclass
class ForgeIndex:
    """Index of cards by what trigger events they produce and respond to."""
    # {trigger_mode: [(card_name, ability_idx, event_detail_dict)]}
    _producers: dict = field(default_factory=lambda: defaultdict(list))
    # {trigger_mode: [(card_name, ability_idx, trigger_filter_str, origin, destination)]}
    _responders: dict = field(default_factory=lambda: defaultdict(list))
    producer_counts: dict = field(default_factory=dict)
    responder_counts: dict = field(default_factory=dict)
    total_cards: int = 0

    def producers_for(self, trigger_mode: str) -> list:
        return self._producers.get(trigger_mode, [])

    def responders_for(self, trigger_mode: str) -> list:
        return self._responders.get(trigger_mode, [])

    def compute_event_idf(self) -> dict:
        """Compute IDF multipliers for producer and responder events."""
        result = {"producer": {}, "responder": {}}
        n = max(self.total_cards, 1)
        max_idf = math.log(n) if n > 1 else 1.0
        min_idf = math.log(2) if n > 2 else 0.1
        span = max_idf - min_idf if max_idf > min_idf else 1.0

        for side, counts in [("producer", self.producer_counts),
                             ("responder", self.responder_counts)]:
            for event, count in counts.items():
                raw = math.log(max(n / max(count, 1), 1))
                normalized = 0.3 + 2.7 * (raw - min_idf) / span
                result[side][event] = round(max(0.3, min(3.0, normalized)), 3)
        return result


def build_forge_index(conn) -> ForgeIndex:
    """Build a ForgeIndex from the forge_abilities table.

    Producers: cards with effect verbs that produce trigger events
    Responders: cards with trigger abilities that respond to events
    """
    idx = ForgeIndex()

    # Count distinct cards
    idx.total_cards = conn.execute(
        "SELECT COUNT(DISTINCT card_name) FROM forge_abilities"
    ).fetchone()[0]

    # Index producers: cards with effect verbs → trigger events they produce
    for row in conn.execute(
        "SELECT card_name, ability_index, verb, target, trigger_origin, trigger_destination "
        "FROM forge_abilities WHERE verb IS NOT NULL"
    ).fetchall():
        card_name, ab_idx, verb, target, origin, dest = row
        events = verb_to_events(verb)
        for event in events:
            mode = event["trigger_mode"]
            detail = {
                "verb": verb,
                "target": target,
                "origin": event.get("origin") or origin,
                "destination": event.get("destination") or dest,
            }
            idx._producers[mode].append((card_name, ab_idx, detail))

    # Index responders: cards with trigger abilities
    for row in conn.execute(
        "SELECT card_name, ability_index, trigger_mode, trigger_filter, "
        "trigger_origin, trigger_destination "
        "FROM forge_abilities WHERE trigger_mode IS NOT NULL"
    ).fetchall():
        card_name, ab_idx, trigger_mode, trigger_filter, origin, dest = row
        idx._responders[trigger_mode].append(
            (card_name, ab_idx, trigger_filter or "", origin or "", dest or "")
        )

    # Compute counts
    for mode, entries in idx._producers.items():
        idx.producer_counts[mode] = len({name for name, _, _ in entries})
    for mode, entries in idx._responders.items():
        idx.responder_counts[mode] = len({name for name, _, _, _, _ in entries})

    return idx
```

- [ ] **Step 4: Run tests, full suite, commit**

```bash
python3 -m pytest tests/test_forge_indexer.py -v
python3 -m pytest tests/ -q --tb=short
git add mtg_synergy/causal/forge_indexer.py tests/test_forge_indexer.py
git commit -m "feat(causal): add Forge-native causal indexer"
```

---

## Task 3: Forge-Native Graph Builder

**Files:**
- Create: `mtg_synergy/causal/forge_graph_builder.py`
- Test: `tests/test_forge_graph_builder.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_forge_graph_builder.py`:

```python
"""Tests for Forge-native causal graph builder."""
import sqlite3
import pytest
from mtg_synergy.causal.forge_graph_builder import build_forge_edges, compute_filter_match
from mtg_synergy.causal.forge_indexer import build_forge_index
from mtg_synergy.causal.types import Edge
from mtg_synergy.parse.forge_import import ensure_forge_schema, parse_forge_card_file, import_card_to_db
from mtg_synergy.parse.forge_filter_parser import parse_forge_filter


# Reuse card fixtures from test_forge_indexer
KRENKO = """Name:Krenko, Mob Boss
ManaCost:2 R R
Types:Legendary Creature Goblin Warrior
PT:3/3
A:AB$ Token | Cost$ T | TokenScript$ r_1_1_goblin | TokenAmount$ X
SVar:X:Count$Valid Goblin.YouCtrl
Oracle:{T}: Create X 1/1 red Goblin creature tokens."""

PURPHOROS = """Name:Purphoros, God of the Forge
ManaCost:3 R
Types:Legendary Enchantment Creature God
PT:6/5
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 2
Oracle:Whenever a creature enters the battlefield under your control, deals 2 damage."""

GOBLIN_LORD = """Name:Goblin Chieftain
ManaCost:1 R R
Types:Creature Goblin
PT:2/2
T:Mode$ ChangesZone | ValidCard$ Goblin.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigPump | TriggerZones$ Battlefield
SVar:TrigPump:DB$ Pump | Defined$ Self | NumAtt$ +1 | NumDef$ +1
Oracle:Whenever a Goblin enters the battlefield under your control, Goblin Chieftain gets +1/+1."""


def _setup(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    for text in [KRENKO, PURPHOROS, GOBLIN_LORD]:
        card = parse_forge_card_file(text)
        import_card_to_db(conn, card)
    conn.commit()
    return conn


def test_filter_match_exact():
    """Goblin.YouCtrl filter matches a Goblin token producer → exact."""
    responder_filter = parse_forge_filter("Goblin.YouCtrl")
    producer_detail = {"verb": "Token", "target": "r_1_1_goblin"}
    match = compute_filter_match(responder_filter, producer_detail, "ChangesZone")
    assert match == "exact"


def test_filter_match_broad():
    """Creature.YouCtrl filter matches any creature token → broad."""
    responder_filter = parse_forge_filter("Creature.YouCtrl")
    producer_detail = {"verb": "Token", "target": "r_1_1_goblin"}
    match = compute_filter_match(responder_filter, producer_detail, "ChangesZone")
    assert match == "broad"


def test_filter_match_unfiltered():
    """No filter → unfiltered."""
    responder_filter = parse_forge_filter("")
    producer_detail = {"verb": "Token"}
    match = compute_filter_match(responder_filter, producer_detail, "ChangesZone")
    assert match == "unfiltered"


def test_build_edges(tmp_db):
    conn = _setup(tmp_db)
    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)
    assert len(edges) > 0
    # Krenko → Purphoros edge should exist (Token → ChangesZone trigger)
    kr_pu = [e for e in edges if e.source == "Krenko, Mob Boss"
             and e.target == "Purphoros, God of the Forge"]
    assert len(kr_pu) >= 1
    conn.close()


def test_goblin_lord_gets_exact_match(tmp_db):
    conn = _setup(tmp_db)
    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)
    # Krenko → Goblin Chieftain should be stronger than Krenko → Purphoros
    kr_lord = [e for e in edges if e.source == "Krenko, Mob Boss"
               and e.target == "Goblin Chieftain"]
    kr_purph = [e for e in edges if e.source == "Krenko, Mob Boss"
                and e.target == "Purphoros, God of the Forge"]
    assert len(kr_lord) >= 1
    assert len(kr_purph) >= 1
    # Exact match (Goblin filter) should have higher strength than broad (Creature filter)
    assert kr_lord[0].strength > kr_purph[0].strength
    conn.close()


def test_no_self_edges(tmp_db):
    conn = _setup(tmp_db)
    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)
    for e in edges:
        assert e.source != e.target, f"Self-edge found: {e.source}"
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement forge_graph_builder.py**

Create `mtg_synergy/causal/forge_graph_builder.py`:

```python
"""Build causal edges between cards using Forge vocabulary.

Matches effect producers against trigger responders using ForgeFilter
matching and IDF weighting. Replaces the old graph_builder.py.
"""
from collections import defaultdict

from mtg_synergy.causal.forge_indexer import ForgeIndex
from mtg_synergy.causal.types import Edge, EdgeDetail
from mtg_synergy.parse.forge_filter_parser import parse_forge_filter
from mtg_synergy.parse.forge_types import ForgeFilter


def compute_filter_match(responder_filter: ForgeFilter, producer_detail: dict,
                         trigger_mode: str) -> str:
    """Determine how well a producer matches a responder's filter.

    Returns: "exact", "broad", "unfiltered", or "none".
    """
    if not responder_filter or not responder_filter.card_types:
        if not responder_filter or (not responder_filter.subtypes and
                                     not responder_filter.controller):
            return "unfiltered"

    # Check subtype match (Goblin.YouCtrl → exact if producer makes Goblins)
    if responder_filter.subtypes:
        # Check if the producer's token/target mentions this subtype
        verb = producer_detail.get("verb", "")
        target = producer_detail.get("target", "") or ""
        target_lower = target.lower()
        for st in responder_filter.subtypes:
            if st.lower() in target_lower:
                return "exact"
        # Subtype required but not matched — still possible as broad
        # if card_type matches
        if responder_filter.card_types:
            return "broad"
        return "none"

    # Card type only (Creature.YouCtrl → broad)
    if responder_filter.card_types:
        return "broad"

    return "unfiltered"


_PRECISION_STRENGTH = {"exact": 1.0, "broad": 0.6, "unfiltered": 0.3, "none": 0.0}


def build_forge_edges(idx: ForgeIndex, max_edges_per_event: int = 50000) -> list[Edge]:
    """Build causal edges from the Forge index.

    For each trigger mode, cross-match producers × responders with
    filter matching and IDF weighting.
    """
    event_idf = idx.compute_event_idf()
    edges = []

    # Get all trigger modes that have both producers and responders
    all_modes = set(idx._producers.keys()) & set(idx._responders.keys())

    for mode in all_modes:
        producers = idx._producers[mode]
        responders = idx._responders[mode]

        # IDF for this event
        p_idf = event_idf["producer"].get(mode, 1.0)
        r_idf = event_idf["responder"].get(mode, 1.0)
        combined_idf = min(p_idf * r_idf, 3.0)

        edge_count = 0
        for prod_name, prod_idx, prod_detail in producers:
            for resp_name, resp_idx, resp_filter_str, resp_origin, resp_dest in responders:
                if prod_name == resp_name:
                    continue

                # Check zone match for ChangesZone
                if mode == "ChangesZone":
                    prod_dest = prod_detail.get("destination", "")
                    if resp_dest and prod_dest and resp_dest != prod_dest:
                        continue
                    prod_orig = prod_detail.get("origin", "")
                    if resp_origin and prod_orig and resp_origin != "Any" and resp_origin != prod_orig:
                        continue

                # Filter matching
                resp_filter = parse_forge_filter(resp_filter_str) if resp_filter_str else ForgeFilter()
                precision = compute_filter_match(resp_filter, prod_detail, mode)
                strength = _PRECISION_STRENGTH.get(precision, 0.0)
                if strength <= 0:
                    continue

                strength *= combined_idf

                edges.append(Edge(
                    source=prod_name,
                    target=resp_name,
                    edge_type="triggers",
                    ability_a=prod_idx,
                    ability_b=resp_idx,
                    strength=strength,
                    detail=EdgeDetail(
                        event=mode,
                        filter_precision=precision,
                    ),
                ))
                edge_count += 1

                if edge_count >= max_edges_per_event:
                    break
            if edge_count >= max_edges_per_event:
                break

    # Dedup: keep strongest edge per (source, target)
    best = {}
    for e in edges:
        key = (e.source, e.target)
        if key not in best or e.strength > best[key].strength:
            best[key] = e

    return list(best.values())
```

- [ ] **Step 4: Run tests, full suite, commit**

```bash
python3 -m pytest tests/test_forge_graph_builder.py -v
python3 -m pytest tests/ -q --tb=short
git add mtg_synergy/causal/forge_graph_builder.py tests/test_forge_graph_builder.py
git commit -m "feat(causal): add Forge-native graph builder with filter matching"
```

---

## Task 4: Wire into build_graph.py + CausalContext

**Files:**
- Modify: `build_graph.py`
- Modify: `mtg_synergy/causal/__init__.py`
- Test: `tests/test_forge_causal_integration.py`

- [ ] **Step 1: Write integration test**

Create `tests/test_forge_causal_integration.py`:

```python
"""End-to-end test: Forge import → index → graph → score."""
import sqlite3
import pytest
from mtg_synergy.parse.forge_import import (
    ensure_forge_schema, parse_forge_card_file, import_card_to_db
)
from mtg_synergy.causal.forge_indexer import build_forge_index
from mtg_synergy.causal.forge_graph_builder import build_forge_edges
from mtg_synergy.causal import ensure_causal_schema


KRENKO = """Name:Krenko, Mob Boss
ManaCost:2 R R
Types:Legendary Creature Goblin Warrior
PT:3/3
A:AB$ Token | Cost$ T | TokenScript$ r_1_1_goblin | TokenAmount$ X
SVar:X:Count$Valid Goblin.YouCtrl
Oracle:{T}: Create X 1/1 red Goblin creature tokens."""

PURPHOROS = """Name:Purphoros, God of the Forge
ManaCost:3 R
Types:Legendary Enchantment Creature God
PT:6/5
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 2
Oracle:Whenever a creature enters, deals 2 damage."""

IMPACT = """Name:Impact Tremors
ManaCost:1 R
Types:Enchantment
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 1
Oracle:Whenever a creature enters, deals 1 damage."""


def test_end_to_end_forge_graph(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    ensure_causal_schema(conn)

    for text in [KRENKO, PURPHOROS, IMPACT]:
        card = parse_forge_card_file(text)
        import_card_to_db(conn, card)
    conn.commit()

    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)

    assert len(edges) > 0

    # Store edges
    from mtg_synergy.causal import store_edges
    count = store_edges(conn, edges)
    assert count > 0

    # Verify we can load and score
    from mtg_synergy.causal import CausalContext
    ctx = CausalContext(conn, "Krenko, Mob Boss", {"Purphoros, God of the Forge"})
    score = ctx.causal_score("Impact Tremors")
    assert score > 0  # Impact Tremors should synergize with Krenko

    conn.close()
```

- [ ] **Step 2: Add store_edges helper to causal/__init__.py**

Add to `mtg_synergy/causal/__init__.py`:

```python
def store_edges(conn, edges: list[Edge]) -> int:
    """Store edges in interaction_edges table."""
    import json as _json
    conn.execute("DELETE FROM interaction_edges")
    for e in edges:
        conn.execute(
            "INSERT OR REPLACE INTO interaction_edges VALUES (?,?,?,?,?,?,?)",
            (e.source, e.target, e.edge_type, e.ability_a, e.ability_b,
             e.strength, _json.dumps(e.detail.to_dict())))
    conn.commit()
    return len(edges)
```

- [ ] **Step 3: Update build_graph.py to support Forge mode**

Add `--forge` flag to build_graph.py:

```python
# In main(), add:
parser.add_argument("--forge", action="store_true", help="Build from Forge data (new system)")

# In the build logic:
if args.forge:
    from mtg_synergy.causal.forge_indexer import build_forge_index
    from mtg_synergy.causal.forge_graph_builder import build_forge_edges
    from mtg_synergy.causal import store_edges
    print("Building Forge-native graph...")
    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)
    count = store_edges(conn, edges)
    print(f"Done: {count} edges built")
```

- [ ] **Step 4: Run tests, full suite, commit**

```bash
python3 -m pytest tests/test_forge_causal_integration.py -v
python3 -m pytest tests/ -q --tb=short
git add mtg_synergy/causal/__init__.py mtg_synergy/causal/forge_graph_builder.py build_graph.py tests/test_forge_causal_integration.py
git commit -m "feat(causal): wire Forge graph builder into build_graph.py"
```

---

## Task 5: Build Forge Graph + Evaluate

Operational task — run the new graph builder and compare to old.

- [ ] **Step 1: Backup old edges**

```bash
python3 -c "
from mtg_synergy.db import get_connection
conn = get_connection()
conn.execute('DROP TABLE IF EXISTS interaction_edges_v2')
conn.execute('CREATE TABLE interaction_edges_v2 AS SELECT * FROM interaction_edges')
conn.commit()
print('Backed up', conn.execute('SELECT COUNT(*) FROM interaction_edges_v2').fetchone()[0], 'edges')
conn.close()
"
```

- [ ] **Step 2: Build Forge-native graph**

```bash
python3 build_graph.py --forge
```

- [ ] **Step 3: Check stats**

```bash
python3 -c "
from mtg_synergy.db import get_connection
conn = get_connection()
edges = conn.execute('SELECT COUNT(*) FROM interaction_edges').fetchone()[0]
sources = conn.execute('SELECT COUNT(DISTINCT source_id) FROM interaction_edges').fetchone()[0]
targets = conn.execute('SELECT COUNT(DISTINCT target_id) FROM interaction_edges').fetchone()[0]
edhrec_with = conn.execute('''
    SELECT COUNT(DISTINCT ecs.card_name) FROM edhrec_card_synergy ecs
    JOIN cards c ON c.name = ecs.card_name
    WHERE c.oracle_id IN (
        SELECT fnm.oracle_id FROM forge_name_map fnm
        JOIN interaction_edges ie ON ie.source_id = fnm.forge_name OR ie.target_id = fnm.forge_name
    ) OR c.name IN (SELECT source_id FROM interaction_edges UNION SELECT target_id FROM interaction_edges)
''').fetchone()[0]
edhrec_total = conn.execute('SELECT COUNT(DISTINCT card_name) FROM edhrec_card_synergy').fetchone()[0]
print(f'Forge graph: {edges:,} edges, {sources:,} sources, {targets:,} targets')
print(f'EDHREC coverage: {edhrec_with}/{edhrec_total} ({edhrec_with/edhrec_total:.0%})')
conn.close()
"
```

- [ ] **Step 4: Run Recall@K evaluation**

```bash
python3 -c "
from mtg_synergy.db import get_connection
from optimize_weights import load_ground_truth, load_commander_info, precompute_scores, evaluate_weights, evaluate_recall
from mtg_synergy.config import SCORING_WEIGHTS

conn = get_connection()
gt = load_ground_truth(conn)
info = load_commander_info(conn)
pre = precompute_scores(conn, gt, info, max_commanders=50)

print('=== Forge-native graph Recall@K ===')
for label, w in [('All signals', {'LLM': 10, 'CAUSAL': 2}),
                  ('LLM only', {'LLM': 10, 'CAUSAL': 0}),
                  ('Causal only', {'LLM': 0, 'CAUSAL': 2})]:
    score, n = evaluate_weights(w, pre, conn=conn)
    print(f'{label}: Recall@100={score:.1%} ({n} commanders)')
evaluate_recall(conn, pre, {'LLM': 10, 'CAUSAL': 2})
conn.close()
"
```

- [ ] **Step 5: Compare old vs new**

If Forge graph Recall@K is better than old (backed up as v2), keep it. If worse, investigate.

- [ ] **Step 6: Commit**

```bash
git add build_graph.py
git commit -m "feat: build Forge-native graph + evaluate Recall@K improvement"
```

---

## Task 6: Update CLAUDE.md + Cleanup

- [ ] **Step 1: Update CLAUDE.md**

Update signal architecture, pipeline docs, file references, test counts.

- [ ] **Step 2: Run final test suite**

```bash
python3 -m pytest tests/ -q --tb=short
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with Forge-native causal graph"
```
